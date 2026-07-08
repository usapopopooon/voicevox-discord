"""読み上げ Bot の Discord adapter と composition root。

Discord のイベント、slash command、実行時キャッシュ、DB I/O、各 feature
パッケージをここで組み立てる。単独で成立するユーザー向け表示は段階的に
``features/*`` へ移し、このファイルは実行時状態を集めて小さな snapshot を
feature へ渡す adapter として残す。
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
import asyncpg
import discord
from aiohttp import web
from app import config, database, launcher
from discord import app_commands
from dotenv import load_dotenv
from features import dictionary as dictionary_feature
from features import internal_tts_api as internal_tts_api_feature
from features import license as license_feature
from features import voice_playback as voice_playback_feature
from features import voice_synthesis as voice_synthesis_feature
from features.dictionary import infrastructure as dictionary_infrastructure
from features.dictionary import text_processing
from features.discord_bot import commands as discord_bot_commands
from features.discord_bot import help as discord_bot_help
from features.discord_bot import lifecycle as discord_bot_lifecycle
from features.discord_bot import messages as discord_bot_messages
from features.discord_bot import ui as discord_bot_ui
from features.mutes import application as mutes_application
from features.mutes import infrastructure as mutes_infrastructure
from features.status import StatusSnapshot
from features.status import build_status_embed as build_status_embed_from_snapshot
from features.voice_sessions import discord_adapter as voice_sessions_discord_adapter
from features.voice_sessions import infrastructure as voice_sessions_infrastructure
from features.voice_settings import infrastructure as voice_settings_infrastructure
from features.voice_synthesis import SynthCacheKey, VoiceSynthesisSettings
from migrations import runner as migration_runner

# ``app.launcher`` はこの module を runtime context として受け取り、ここから
# 必要な module を読む。import linter からも公開 context の一部だと分かるよう、
# 利用される symbol を明示的な tuple にまとめておく。
try:
    import emoji as emoji_lib
except ImportError:  # pragma: no cover
    # 依存がない環境では既存の簡易置換にフォールバック
    emoji_lib = None

load_dotenv()

# ログ設定（本番では LOG_LEVEL=WARNING 等でログ量を絞ってストレージ課金を節約）
_LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
# 複数Botモードで子プロセスのログを区別するためのインスタンス番号（"1" が単一/親）
_LOG_INSTANCE_INDEX = os.getenv("BOT_INSTANCE_INDEX", "1")
_LOG_FORMAT = (
    f"%(asctime)s [%(levelname)s] [bot#{_LOG_INSTANCE_INDEX}] %(name)s: %(message)s"
)
logging.basicConfig(level=_LOG_LEVEL, format=_LOG_FORMAT)
logger = logging.getLogger(__name__)
if _LOG_LEVEL_NAME not in logging.getLevelNamesMapping():
    logger.warning(f"LOG_LEVEL='{_LOG_LEVEL_NAME}' は未知のため INFO にフォールバック")


def _runtime_context() -> Any:
    """feature adapter へ渡す一時的な runtime context を返す。

    Python 側の移行中は、既存テストと adapter wrapper を安定させるために
    この module 自体を composition context として使っている。この動的な詳細は
    1 箇所に閉じ込め、TypeScript 化時は明示的な ``AppContext``
    object/interface へ置き換える。
    """
    # 重要: feature 側へ直接 `sys.modules[__name__]` を渡す呼び出しを散らすと、
    # どこが移行期の動的 context なのか追いにくくなる。必ずこの関数経由にする。
    return sys.modules[__name__]


def _new_trace_id() -> str:
    """短い追跡IDを作る。ログ上で1操作の流れを追いやすくする。"""
    return uuid.uuid4().hex[:12]


def _json_default(value: object) -> str:
    """構造化ログ field 用のフォールバック serializer。

    引数:
        value: ``json.dumps`` がそのままでは直列化できない object。

    戻り値:
        人間がログ調査で読める文字列表現。
    """
    return str(value)


def _log_event(level: int, event: str, /, **fields: object) -> None:
    """検索しやすい event 名付きログを出す。

    本格的な JSON logger へ差し替える前段として、既存ログ設定に乗せたまま
    `event=... fields={...}` の形に揃える。
    """
    # None は「値がない」だけで調査情報として弱いので出さない。ログ検索時に
    # field の有無が意味を持つよう、意図的に落としている。
    safe_fields = {key: value for key, value in fields.items() if value is not None}
    logger.log(
        level,
        "event=%s fields=%s",
        event,
        json.dumps(safe_fields, ensure_ascii=False, default=_json_default),
    )


@dataclass(frozen=True)
class RecentError:
    """運営側の調査に使う小さなインメモリエラー記録。

    属性:
        event: ログで使う安定した event 名。
        message: 人間が読めるエラーメッセージ。
        trace_id: event ログと共有する相関 ID。
        happened_at: エラー記録時点の UNIX timestamp。
        guild_id: エラーが起きた Discord guild。該当しない場合は ``None``。
    """

    event: str
    message: str
    trace_id: str
    happened_at: float
    guild_id: int | None = None


_recent_errors: deque[RecentError] = deque(maxlen=20)


def _record_recent_error(
    event: str, message: str, trace_id: str, *, guild_id: int | None = None
) -> None:
    """運営側だけが使う短命なエラー概要を保存する。

    引数:
        event: 構造化ログにも出る安定した event 名。
        message: 人間が読めるエラーメッセージ。
        trace_id: 同じ操作をログ上で追うための相関 ID。
        guild_id: 失敗が起きた Discord guild。該当しない場合は ``None``。
    """
    _recent_errors.append(
        RecentError(
            event=event,
            message=message,
            trace_id=trace_id,
            happened_at=time.time(),
            guild_id=guild_id,
        )
    )


# 設定（環境変数で切り替え）
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_TOKENS_RAW = os.getenv("DISCORD_TOKENS", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_SPEAKER = int(os.getenv("DEFAULT_SPEAKER_ID", "46"))


def _env_flag(name: str, default: bool = False) -> bool:
    """真偽値の環境変数を解釈する。"""
    return config.env_flag(_runtime_context(), name, default)


def _resolve_discord_tokens() -> list[str]:
    """DISCORD_TOKENS / DISCORD_TOKEN から起動対象トークン一覧を作る。"""
    return config.resolve_discord_tokens(_runtime_context())


RUN_DB_MIGRATIONS = _env_flag("RUN_DB_MIGRATIONS", default=True)
IS_MULTIBOT_CHILD = _env_flag("MULTIBOT_CHILD", default=False)
BOT_INSTANCE_INDEX = _LOG_INSTANCE_INDEX
_migrations_ran = False

# 複数Bot時は1Postgresへ N台ぶん接続するため、環境変数で個別に絞れるようにする
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "5"))

# TTS の待ち時間上限（ハング時の体感遅延を抑える）
TTS_AUDIO_QUERY_TIMEOUT_SECONDS = float(
    os.getenv("TTS_AUDIO_QUERY_TIMEOUT_SECONDS", "3")
)
TTS_SYNTHESIS_TIMEOUT_SECONDS = float(os.getenv("TTS_SYNTHESIS_TIMEOUT_SECONDS", "8"))
TTS_SPEAKERS_TIMEOUT_SECONDS = float(os.getenv("TTS_SPEAKERS_TIMEOUT_SECONDS", "3"))

# 同じ private Docker network 上の sibling service 向け内部 API。
INTERNAL_TTS_API_ENABLED = _env_flag("INTERNAL_TTS_API_ENABLED", default=False)
INTERNAL_TTS_API_HOST = os.getenv("INTERNAL_TTS_API_HOST", "0.0.0.0")
INTERNAL_TTS_API_PORT = int(os.getenv("INTERNAL_TTS_API_PORT", "8080"))
INTERNAL_TTS_API_TOKEN = os.getenv("INTERNAL_TTS_API_TOKEN", "").strip()
INTERNAL_TTS_API_MAX_TEXT_LENGTH = int(
    os.getenv("INTERNAL_TTS_API_MAX_TEXT_LENGTH", "120")
)

# 子プロセスの自動再起動（指数バックオフ + クラッシュループ検出）
BOT_RESTART_BACKOFF_MAX_SECONDS = 60
BOT_CRASH_WINDOW_SECONDS = 300
BOT_CRASH_THRESHOLD = 5
BOT_POLL_INTERVAL_SECONDS = 2


def _engine_url(
    env_name: str,
    default: str = "",
    *,
    profile: str | None = None,
    profile_default: str = "",
) -> str:
    return config.engine_url(
        _runtime_context(),
        env_name,
        default,
        profile=profile,
        profile_default=profile_default,
    )


# 各エンジンの定義（名前, URL, IDオフセット）
# IDオフセットでエンジン間のスピーカーID衝突を回避
_configured_engines: list[tuple[str, str, int]] = [
    ("VOICEVOX", _engine_url("VOICEVOX_URL", "http://localhost:50021"), 0),
    (
        "COEIROINK",
        _engine_url(
            "COEIROINK_URL",
            profile="coeiroink",
            profile_default="http://coeiroink:50031",
        ),
        10000,
    ),
    (
        "SHAREVOX",
        _engine_url(
            "SHAREVOX_URL",
            profile="sharevox",
            profile_default="http://sharevox:50025",
        ),
        20000,
    ),
]
ENGINES: list[tuple[str, str, int]] = [
    (name, url, offset) for name, url, offset in _configured_engines if url
]

logger.info(f"TTS_ENGINES: {[(n, u) for n, u, _ in ENGINES]}")
logger.info(f"DEFAULT_SPEAKER_ID: {DEFAULT_SPEAKER}")

# Intents設定（message_contentはテキスト読み上げに必須）
# 使っていないイベント・キャッシュ起因のメモリを抑えるため不要 intent を
# 明示的に OFF にする。
intents = discord.Intents.default()
intents.message_content = True
intents.typing = False
intents.guild_reactions = False
intents.dm_reactions = False
# DM メッセージは `on_message` で `if not message.guild: return` で弾いており、
# Bot として DM 機能を提供していないため OFF。将来 DM 経由の機能を追加する
# 場合はここを True に戻すこと。
intents.dm_messages = False
intents.bans = False
intents.integrations = False
intents.invites = False
intents.webhooks = False
intents.emojis_and_stickers = False
intents.guild_scheduled_events = False
intents.auto_moderation = False

# 共有 HTTP セッション（Keep-Alive で接続再利用）
_http_session: aiohttp.ClientSession | None = None

# VC 切断復旧用の runtime 状態。永続化された active_voice_sessions と組み合わせ、
# deploy や Discord gateway 復帰時は再接続し、パネル操作の切断は復帰しない。
_restored_voice_sessions = False
_shutting_down = False
_self_voice_recovery_tasks: dict[int, asyncio.Task[None]] = {}
_ready_voice_restore_task: asyncio.Task[None] | None = None
_last_gateway_recoverable_disconnect_at: float | None = None
_last_user_requested_disconnect_at_by_guild: dict[int, float] = {}
_gateway_recoverable_disconnect_log_handler: logging.Handler | None = None
_gateway_recoverable_disconnect_loggers: tuple[logging.Logger, ...] = ()


async def get_http_session() -> aiohttp.ClientSession:
    """共有 ClientSession を返す（未作成/クローズ済みなら新規作成）"""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close_http_session():
    """共有 ClientSession をクローズする"""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


class TtsClient(discord.Client):
    """discord.Client のサブクラス。終了時に共有 HTTP セッションも閉じる。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """gateway 復旧ログの監視を取り付けて Discord client を初期化する。"""
        super().__init__(*args, **kwargs)
        voice_sessions_discord_adapter.install_gateway_recoverable_disconnect_log_handler(
            _runtime_context()
        )

    async def setup_hook(self) -> None:
        """gateway 接続前に、再起動後も受けたい persistent view を登録する。"""
        register_persistent_views()

    async def close(self) -> None:
        # discord.py の close は gateway だけを見るため、Bot が別途持つ
        # aiohttp server/client をここで一緒に閉じる。終了順序を逆にすると、
        # 停止中に内部 API が合成リクエストを受ける可能性がある。
        voice_sessions_discord_adapter.begin_shutdown(_runtime_context())
        voice_sessions_discord_adapter.uninstall_gateway_recoverable_disconnect_log_handler(
            _runtime_context()
        )
        try:
            await stop_internal_tts_api()
            await close_http_session()
        finally:
            await super().close()


# chunk_guilds_at_startup=False: 起動時に全ギルドのメンバーを一括キャッシュしない
# （大規模ギルドで数十〜数百MB節約）。必要時は guild.get_member() で参照し、
# キャッシュミスは None 許容のため現状コードと互換。
# max_messages: 受信メッセージのキャッシュ件数。デフォルト 1000 件は読み上げ用途に
# 過剰で message_content=True と相まってメモリを大きく食うため小さく絞る。
# 編集/削除イベントのキャッシュ参照は本Botでは利用していない。
DISCORD_MESSAGE_CACHE_SIZE = int(os.getenv("DISCORD_MESSAGE_CACHE_SIZE", "100"))
client = TtsClient(
    intents=intents,
    chunk_guilds_at_startup=False,
    max_messages=DISCORD_MESSAGE_CACHE_SIZE,
)
tree = app_commands.CommandTree(client)
_internal_tts_api_runner: web.AppRunner | None = None
_persistent_views_registered = False

# ギルドあたりの再生キュー最大長。スパム時は新規メッセージ側を drop して、
# 「読み上げが何分も遅れて続く」体感遅延を抑える（小さい値ほど追従性が良い）。
QUEUE_MAXLEN = 4

# ギルドごとの再生キューと読み上げ対象チャンネル
queues: dict[int, deque[bytes]] = {}
read_channels: dict[int, int] = {}  # guild ID から channel ID への対応
play_locks: dict[int, asyncio.Lock] = {}  # guild ID -> 再生開始の競合防止ロック
# ギルド内での「合成 → queue 追加」順序を保証するロック。
# 複数メッセージが同時到着した時、短文が先に合成完了して順序が逆転する race を防ぐ。
# 代償としてギルド内は合成がシリアライズされる（ギルド間は並行）。
synth_order_locks: dict[int, asyncio.Lock] = {}
engine_error_notified_at: dict[int, float] = {}  # guild ID -> monotonic 秒
ENGINE_ERROR_NOTIFY_INTERVAL = 30.0

# 起動時 VC 復旧（_restore_voice_sessions_on_startup）の多重起動防止 + リトライ設定
_vc_reconnect_inflight: set[int] = set()
VC_RECONNECT_MAX_ATTEMPTS = 5
VC_RECONNECT_BACKOFF_BASE_SECONDS = 2
VC_RECONNECT_BACKOFF_MAX_SECONDS = 60

# fire-and-forget タスクの参照保持（CPython の GC で消されないように）
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """create_task しつつ参照を保持し、完了時に自動回収する。"""
    task = asyncio.create_task(coro)
    # fire-and-forget task は参照がないと警告や GC タイミングが読みづらくなる。
    # セットに保持しておけば、未完了 task の存在もデバッグ時に追いやすい。
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _new_queue() -> deque[bytes]:
    """voice-playback feature 経由で上限付き音声キューを作る。"""
    return voice_playback_feature.new_queue(_runtime_context())


def _ensure_queue(guild_id: int) -> deque[bytes]:
    """voice-playback feature 経由で guild のキューを返す。"""
    return voice_playback_feature.ensure_queue(_runtime_context(), guild_id)


def _cleanup_guild_state(guild_id: int) -> None:
    """voice-playback feature 経由で再生状態をすべて消す。"""
    voice_playback_feature.cleanup_guild_state(_runtime_context(), guild_id)


def _cleanup_guild_playback_state(guild_id: int) -> None:
    """読み上げ対象を残したまま一時的な再生状態だけを消す。"""
    voice_playback_feature.cleanup_guild_playback_state(_runtime_context(), guild_id)


def _can_start_playback(vc: discord.VoiceClient) -> bool:
    """再生開始可能な VC 状態かを安全に判定する。

    接続状態の遷移レースで discord.ClientException が出ることがあるため、
    その場合は再生不可として扱う。
    """
    try:
        return vc.is_connected() and not vc.is_playing() and not vc.is_paused()
    except discord.ClientException as e:
        logger.info(f"VC状態確認をスキップ（接続遷移中）: {e}")
        return False


def can_start_playback(vc: discord.VoiceClient) -> bool:
    """feature adapter 用の公開 playback context wrapper。"""
    # feature の Protocol には public 名だけを見せる。既存テスト互換のため
    # `_can_start_playback` は残すが、新しい feature からはこの名前を使う。
    return _can_start_playback(vc)


def _is_vc_connected(vc: discord.VoiceClient) -> bool:
    """VC が接続中かを安全に判定する。

    遷移中の `discord.ClientException` を含め、判定中に何らかの例外が出た場合は
    呼び出し側のコマンドハンドラを巻き添えにしないよう「未接続」として扱う。
    """
    try:
        return vc.is_connected()
    except Exception:
        return False


def is_voice_client_connected(vc: discord.VoiceClient) -> bool:
    """feature adapter 用の公開 playback context wrapper。"""
    # `DiscordPlaybackContext` 用の公開名。private helper へ直接依存させない。
    return _is_vc_connected(vc)


def _is_vc_playing(vc: discord.VoiceClient) -> bool:
    """VC が再生中かを安全に判定する（遷移中の ClientException を吸収）"""
    try:
        return vc.is_playing()
    except discord.ClientException:
        return False


async def _safe_disconnect(vc: discord.VoiceClient | None) -> None:
    """VC切断。既に切断済みなどで例外が出ても無視する。"""
    if vc is None:
        return
    try:
        await vc.disconnect(force=False)
    except Exception as e:
        logger.warning(f"切断でエラー: {e}")


def _as_voice_client(
    vc: discord.VoiceProtocol | None,
) -> discord.VoiceClient | None:
    """`guild.voice_client` (VoiceProtocol) を本Botの実体である VoiceClient として扱う。

    本Bot は `channel.connect()` でしか VC を張らないため、実行時は常に
    VoiceClient (または None) になる。型システム上は VoiceProtocol が返る
    ため、ここで一括キャストしてヘルパーに渡せるようにする。
    """
    if vc is None:
        return None
    return cast(discord.VoiceClient, vc)


def _has_active_voice_connection(guild: discord.Guild) -> bool:
    """guild に現在実接続している VC があるかを判定する。

    `guild.voice_client` は切断後も残骸として None でない場合があるため、
    オブジェクトの有無だけでなく `is_connected()` まで確認する。
    """
    vc = _as_voice_client(guild.voice_client)
    return vc is not None and _is_vc_connected(vc)


async def _reset_voice_state(guild: discord.Guild) -> None:
    """guild の VC 接続を無効化し、メモリ状態を完全に掃除する。

    呼び出し側で `_has_active_voice_connection` を False と確認した分岐
    （= 切断済みのはずの状態）で呼び、stale な voice_client 残骸 + 残留キュー
    を一括で初期化するための helper。
    """
    stale = _as_voice_client(guild.voice_client)
    if stale is not None:
        logger.info(f"stale voice_client を掃除 guild={guild.id}")
        await _safe_disconnect(stale)
    _cleanup_guild_state(guild.id)


async def _require_guild_interaction(
    interaction: discord.Interaction,
) -> discord.Guild | None:
    """ギルド内コマンドかを確認し、DM実行時はメッセージを返して中断する。"""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ利用できます"
        )
        return None
    return guild


@dataclass
class VoiceSettings:
    speaker_id: int = DEFAULT_SPEAKER
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    volume: float = 1.0


# メモリキャッシュ
user_settings: dict[tuple[int, int], VoiceSettings] = {}
speakers_cache: dict[int, str] = {}  # global_id -> 表示名
# グローバル話者 ID -> (エンジン URL, エンジン内の実話者 ID)
speaker_engine: dict[int, tuple[str, int]] = {}
# キャラクター名 -> [(global_id, スタイル名)]
characters: dict[str, list[tuple[int, str]]] = {}
guild_dicts: dict[int, dict[str, str]] = {}
guild_mutes: dict[int, set[int]] = {}  # guild ID -> ミュート中 user ID set
_speaker_fetch_success_engines: set[str] = set()


async def _respond(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
) -> None:
    """Interaction に返信する。既に応答済みなら followup に回す。"""
    is_done = False
    is_done_fn = getattr(interaction.response, "is_done", None)
    if callable(is_done_fn):
        is_done = bool(is_done_fn())
    send_kwargs: dict[str, Any] = {"ephemeral": ephemeral}
    if content is not None:
        send_kwargs["content"] = content
    if embed is not None:
        send_kwargs["embed"] = embed
    if view is not None:
        send_kwargs["view"] = view
    if is_done:
        await interaction.followup.send(**send_kwargs)
        return
    await interaction.response.send_message(**send_kwargs)


def _voice_settings_lines(settings: VoiceSettings) -> list[str]:
    speaker_name = speakers_cache.get(settings.speaker_id, f"ID: {settings.speaker_id}")
    return [
        f"キャラクター: {speaker_name}",
        f"話速: {settings.speed}",
        f"音高: {settings.pitch}",
        f"抑揚: {settings.intonation}",
        f"音量: {settings.volume}",
    ]


def _engine_name_for_speaker_id(speaker_id: int) -> str | None:
    """グローバル話者 ID から設定済みエンジン名を解決する。

    引数:
        speaker_id: ユーザー設定に保存されているグローバル話者 ID。

    戻り値:
        話者 mapping が分かる場合は ``"VOICEVOX"`` などのエンジン名。
        不明な場合は ``None``。
    """
    mapping = speaker_engine.get(speaker_id)
    if mapping is None:
        return None
    engine_url, _ = mapping
    for engine_name, configured_url, _ in ENGINES:
        if configured_url == engine_url:
            return engine_name
    return None


def _credit_for_speaker_id(speaker_id: int) -> license_feature.CurrentCredit:
    """実行時の話者 cache から license feature 用のクレジット情報を作る。

    引数:
        speaker_id: ユーザー設定に保存されているグローバル話者 ID。

    戻り値:
        license feature が表示に使えるクレジット metadata。
    """
    raw_speaker_name = speakers_cache.get(speaker_id, f"ID: {speaker_id}")
    return license_feature.credit_for_speaker(
        speaker_id,
        raw_speaker_name,
        engine_name=_engine_name_for_speaker_id(speaker_id),
    )


def _build_license_embed(
    *,
    guild_id: int | None = None,
    user_id: int | None = None,
) -> discord.Embed:
    """コマンドまたはパネル interaction 用の license embed を作る。

    引数:
        guild_id: リクエスト者の設定解決に使う guild。不要な場合は ``None``。
        user_id: リクエスト者の設定解決に使う user。不要な場合は ``None``。

    戻り値:
        license feature が描画した Discord embed。両方の ID がある場合は、
        現在の話者クレジット候補も含める。
    """
    current: license_feature.CurrentCredit | None = None
    if guild_id is not None and user_id is not None:
        settings = get_user_settings(guild_id, user_id)
        current = _credit_for_speaker_id(settings.speaker_id)
    return license_feature.build_license_embed(current)


def _panel_license_lines() -> tuple[str, ...]:
    """control panel snapshot に載せる短いライセンス案内を返す。"""
    engine_names = " / ".join(info.engine for info in license_feature.LICENSE_INFOS)
    return (
        f"対応エンジン: {engine_names}",
        "詳細とクレジット表記は「ライセンス」で確認できます",
    )


def _build_status_embed(guild: discord.Guild) -> discord.Embed:
    """実行時状態を集め、公開してよい status embed を描画する。

    引数:
        guild: Bot 状態を表示する Discord guild。

    戻り値:
        公開可能な状態だけを含む status embed。debug 専用情報は運営ログ側に残す。
    """
    guild_id = guild.id
    vc = _as_voice_client(guild.voice_client)
    connected = vc is not None and _is_vc_connected(vc)
    channel = getattr(vc, "channel", None) if vc is not None else None
    return build_status_embed_from_snapshot(
        StatusSnapshot(
            connected=connected,
            voice_channel_name=getattr(channel, "name", "未接続"),
            read_channel_id=read_channels.get(guild_id),
            queue_length=len(queues.get(guild_id, [])),
            queue_maxlen=QUEUE_MAXLEN,
            speaker_count=len(speakers_cache),
            configured_engines=tuple(name for name, _, _ in ENGINES),
            healthy_engines=tuple(sorted(_speaker_fetch_success_engines)),
        ),
    )


# テキスト正規化は features.dictionary.text_processing が所有する。
# ここから下の uppercase alias は「新しい設計」ではなく「移行期の公開面」。
# 既存テストや外部 import が `bot._KAOMOJI_DICT` などを触るため、feature 側の
# 実体と bot.py 側の互換名を同期する。新規コードは text_processing の公開関数を使う。
URL_PATTERN = text_processing.URL_PATTERN
EMAIL_PATTERN = text_processing.EMAIL_PATTERN
CUSTOM_EMOJI_PATTERN = text_processing.CUSTOM_EMOJI_PATTERN
MAX_READ_LENGTH = text_processing.MAX_READ_LENGTH
_text_processing_state = text_processing.export_runtime_state()
_KAOMOJI_DICT = _text_processing_state.kaomoji_dict
_KAOMOJI_PATTERN = _text_processing_state.kaomoji_pattern
_READING_CORRECTIONS = _text_processing_state.reading_corrections
_ENGLISH_WORD_READINGS = _text_processing_state.english_word_readings
_DEFAULT_READING_CORRECTIONS = _text_processing_state.default_reading_corrections
_DEFAULT_ENGLISH_WORD_READINGS = _text_processing_state.default_english_word_readings


def _sync_text_processing_state_to_module() -> None:
    """互換 alias を text-processing module 側へ戻す。

    ``bot._...`` の globals を直接変更する古いテスト/呼び出し元のための橋渡し。
    実際の text-processing 振る舞いは dictionary feature が所有し、この関数は
    feature 関数の呼び出し前に互換状態を owner 側へ押し戻すだけにする。
    """
    current_state = text_processing.export_runtime_state()
    # default_* は immutable MappingProxyType なので、bot.py 側で上書きさせない。
    # runtime dict と compiled pattern だけを互換 alias から戻す。
    text_processing.import_runtime_state(
        text_processing.TextProcessingRuntimeState(
            kaomoji_dict=_KAOMOJI_DICT,
            kaomoji_pattern=_KAOMOJI_PATTERN,
            reading_corrections=_READING_CORRECTIONS,
            english_word_readings=_ENGLISH_WORD_READINGS,
            default_reading_corrections=current_state.default_reading_corrections,
            default_english_word_readings=current_state.default_english_word_readings,
            reading_pattern=current_state.reading_pattern,
            english_word_pattern=current_state.english_word_pattern,
        )
    )


def _sync_text_processing_state_from_module() -> None:
    """feature 側で pattern を再構築した後に互換 alias を更新する。

    ``_KAOMOJI_PATTERN`` はテストが直接変更し得る legacy alias。
    runtime state を最新に保ちつつ Pylance に uppercase module 変数の
    直接再代入と見なされないよう、``globals()`` 経由で更新する。
    """
    state = text_processing.export_runtime_state()
    # Pylance は uppercase への直接代入を「定数再定義」とみなす。globals 経由なら
    # 互換 alias を最新化しつつ strict 設定を壊さない。
    globals()["_KAOMOJI_PATTERN"] = state.kaomoji_pattern


def _rebuild_kaomoji_patterns() -> None:
    """alias 変更後に顔文字 lookup pattern を再構築する。"""
    _sync_text_processing_state_to_module()
    text_processing.rebuild_kaomoji_patterns()
    _sync_text_processing_state_from_module()


def _rebuild_reading_patterns() -> None:
    """実行時辞書の変更後に読み補正 pattern を再構築する。"""
    _sync_text_processing_state_to_module()
    text_processing.rebuild_reading_patterns()


def apply_reading_corrections(text: str) -> str:
    """built-in の漢字読み補正と英単語読み補正を適用する。"""
    # DB から built-in 読みをロードした後など、bot.py 側の互換 dict が最新の
    # 実体になっている場合がある。呼び出し直前に feature 側へ寄せる。
    _sync_text_processing_state_to_module()
    return text_processing.apply_reading_corrections(text)


def clean_text(text: str) -> str:
    """辞書置換と合成の前に Discord message を正規化する。"""
    # 顔文字テストが bot._KAOMOJI_PATTERN を直接差し替えるため、ここでも同期する。
    # 将来的に互換 alias を削除できたら、この同期も消せる。
    _sync_text_processing_state_to_module()
    return text_processing.clean_text(text)


# DB接続プール
db_pool: asyncpg.Pool | None = None
db_init_lock = asyncio.Lock()

# 辞書 feature が所有する置換パターンキャッシュ。
# 既存テスト互換のため bot._dict_patterns として alias も残す。
_dict_patterns: dict[int, re.Pattern[str]] = dictionary_feature.DICT_PATTERN_CACHE

# 合成結果の LRU キャッシュ（cache=True でのみ使用）
# 1件あたり最大 ~500KB。max=32 で ~16MB に抑制。挨拶・入退室通知など
# 繰り返し呼ばれる定型文は十分にヒットする。
# key には話者・速度・ピッチなども含める。音声設定が違うユーザー間で
# bytes を共有すると「別人の声/速度で再生される」事故になるため。
_synth_cache: OrderedDict[SynthCacheKey, bytes] = OrderedDict()
_SYNTH_CACHE_MAX = 32
# 短時間の重複合成を抑える TTL キャッシュ（cache=False でも利用）
# 1件あたり最大 ~500KB。max=16 で ~8MB 上限。
# Discord の同時投稿では同じ文面が短時間に重なることがある。永続 LRU に
# 入れたくない通常読み上げでも、この TTL だけでエンジン負荷をかなり抑えられる。
_recent_synth_cache: OrderedDict[SynthCacheKey, tuple[float, bytes]] = OrderedDict()
_RECENT_SYNTH_CACHE_MAX = 16
_RECENT_SYNTH_TTL_SECONDS = float(os.getenv("RECENT_SYNTH_TTL_SECONDS", "45"))
# 同じキーで同時に合成が走らないよう in-flight 管理
# 値は「先行リクエスト完了」を知らせる Event。後続はここで待つことで、
# キャッシュミス直後の多重 HTTP request を防ぐ。
_synth_in_flight: dict[SynthCacheKey, asyncio.Event] = {}
# 失敗した合成候補の短期バックオフ（engine_url, real_id）-> monotonic deadline
# 複数エンジン/話者候補がある時、落ちている候補へ毎メッセージ突撃しないため。
_candidate_fail_until: dict[tuple[str, int], float] = {}
CANDIDATE_FAIL_BACKOFF_SECONDS = 10.0


def _prune_candidate_fail_until() -> None:
    """期限切れの候補バックオフ entry を削除して dict 肥大化を防ぐ"""
    now = time.monotonic()
    expired = [k for k, deadline in _candidate_fail_until.items() if deadline <= now]
    for k in expired:
        _candidate_fail_until.pop(k, None)


# ユーザ単位レートリミット（トークンバケット）。TTS コスト爆発と abuse 対策。
# CAPACITY=10, 30 秒で満タンに戻る → 1ユーザあたり瞬発 10 件 / 平均 0.33 件/秒 が上限。
USER_RATE_LIMIT_CAPACITY = 10
USER_RATE_LIMIT_REFILL_PER_SEC = USER_RATE_LIMIT_CAPACITY / 30.0
# (guild_id, user_id) -> (残トークン, 最終更新時刻[monotonic])
_user_buckets: dict[tuple[int, int], tuple[float, float]] = {}


def _rate_limit_try_consume(guild_id: int, user_id: int) -> bool:
    """レートリミットでトークンを 1 つ消費。失敗時は False（= 合成スキップ）。"""
    key = (guild_id, user_id)
    now = time.monotonic()
    tokens, last_refill = _user_buckets.get(key, (float(USER_RATE_LIMIT_CAPACITY), now))
    elapsed = now - last_refill
    # Token bucket: 経過時間ぶんだけ回復し、上限を超えないよう cap する。
    # wall clock ではなく monotonic を使い、NTP 補正や時刻変更の影響を避ける。
    tokens = min(
        float(USER_RATE_LIMIT_CAPACITY),
        tokens + elapsed * USER_RATE_LIMIT_REFILL_PER_SEC,
    )
    if tokens < 1.0:
        # 失敗時も last_refill を進める。古い時刻を残すと、次回に過剰回復する。
        _user_buckets[key] = (tokens, now)
        return False
    _user_buckets[key] = (tokens - 1.0, now)
    return True


# speaker_engine 空時の再取得を間引く
_speaker_refresh_lock = asyncio.Lock()
_last_speaker_refresh_attempt = 0.0
SPEAKER_REFRESH_INTERVAL = 30.0


def _require_db_pool() -> asyncpg.Pool:
    """既存呼び出し元とテスト向けに初期化済み DB pool を返す。"""
    return database.require_pool(db_pool)


def _invalidate_dict_cache(guild_id: int) -> None:
    """dictionary feature 経由で guild 1 件分の辞書 cache を無効化する。"""
    dictionary_feature.invalidate_cache(guild_id)


# --- DB 接続と feature infrastructure facade ---

# このセクションは「DB の責務を bot.py に戻す場所」ではない。
# 既存コマンド/テストが bot.load_user_settings() などを import するため、
# feature の infrastructure を呼ぶ薄い facade だけを残している。
# SQL 本文は各 feature の infrastructure.py が所有する。


async def init_db() -> None:
    """DB pool を初期化し、各 feature に自分の schema を保証させる。"""
    await database.init(_runtime_context())


async def load_user_settings() -> None:
    """voice-settings feature の状態をメモリへ読み込む。"""
    await voice_settings_infrastructure.load_user_settings(
        _require_db_pool(),
        user_settings,
        settings_factory=VoiceSettings,
        logger=logger,
    )


async def save_user_setting(
    guild_id: int, user_id: int, settings: VoiceSettings
) -> None:
    """voice-settings feature の行を 1 件保存する。"""
    await voice_settings_infrastructure.save_user_setting(
        _require_db_pool(),
        guild_id=guild_id,
        user_id=user_id,
        settings=settings,
    )


async def load_guild_dicts() -> None:
    """dictionary feature の状態をメモリへ読み込む。"""
    await dictionary_infrastructure.load_guild_dicts(
        _require_db_pool(),
        guild_dicts,
        to_katakana=dictionary_feature.to_katakana,
        logger=logger,
    )


async def load_builtin_reading_dicts() -> None:
    """built-in dictionary feature の状態をメモリへ読み込む。"""
    await dictionary_infrastructure.load_builtin_reading_dicts(
        _require_db_pool(),
        reading_corrections=_READING_CORRECTIONS,
        english_word_readings=_ENGLISH_WORD_READINGS,
        default_reading_corrections=_DEFAULT_READING_CORRECTIONS,
        default_english_word_readings=_DEFAULT_ENGLISH_WORD_READINGS,
        to_katakana=dictionary_feature.to_katakana,
        rebuild_reading_patterns=_rebuild_reading_patterns,
        logger=logger,
    )


def _is_builtin_duplicate(word: str, reading: str) -> bool:
    """辞書行が built-in 辞書の状態と重複するかを返す。"""
    return dictionary_feature.is_builtin_duplicate(
        word, reading, _READING_CORRECTIONS, _ENGLISH_WORD_READINGS
    )


async def add_dict_entry(guild_id: int, word: str, reading: str) -> bool:
    """dictionary feature の行を 1 件保存する。"""
    return await dictionary_infrastructure.add_dict_entry(
        _require_db_pool(),
        guild_dicts,
        guild_id=guild_id,
        word=word,
        reading=reading,
        reading_corrections=_READING_CORRECTIONS,
        english_word_readings=_ENGLISH_WORD_READINGS,
        to_katakana=dictionary_feature.to_katakana,
    )


async def purge_builtin_duplicates_from_user_dicts() -> int:
    """built-in と重複する dictionary feature の行を削除する。"""
    return await dictionary_infrastructure.purge_builtin_duplicates_from_user_dicts(
        _require_db_pool(),
        guild_dicts,
        reading_corrections=_READING_CORRECTIONS,
        english_word_readings=_ENGLISH_WORD_READINGS,
        logger=logger,
    )


async def delete_dict_entry(guild_id: int, word: str) -> None:
    """dictionary feature の行を 1 件削除する。"""
    await dictionary_infrastructure.delete_dict_entry(
        _require_db_pool(),
        guild_dicts,
        guild_id=guild_id,
        word=word,
    )


def apply_dict(guild_id: int, text: str) -> str:
    """dictionary feature の置換を text に適用する。"""
    return dictionary_feature.apply_dictionary(guild_id, text, guild_dicts)


async def load_guild_mutes() -> None:
    """mute feature の状態をメモリへ読み込む。"""
    await mutes_infrastructure.load_guild_mutes(
        _require_db_pool(), guild_mutes, logger=logger
    )


async def add_mute(guild_id: int, user_id: int) -> None:
    """mute feature の行を 1 件追加する。"""
    await mutes_infrastructure.add_mute(
        _require_db_pool(), guild_mutes, guild_id=guild_id, user_id=user_id
    )


async def remove_mute(guild_id: int, user_id: int) -> None:
    """mute feature の行を 1 件削除する。"""
    await mutes_infrastructure.remove_mute(
        _require_db_pool(), guild_mutes, guild_id=guild_id, user_id=user_id
    )


def is_muted(guild_id: int, user_id: int) -> bool:
    """mute feature 経由でユーザーがミュート中かを返す。"""
    return mutes_application.is_muted(guild_mutes, guild_id, user_id)


# --- VC セッション永続化（再起動・切断時の復旧用）---

# voice_sessions は「Bot がどの VC とテキストチャンネルにいたか」だけを扱う。
# 接続の実処理は Discord adapter、永続化は infrastructure に分けることで、
# DB テストと Discord mock テストを別々に読めるようにしている。


async def record_voice_session(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """voice-session feature の行を 1 件保存する。"""
    await voice_sessions_infrastructure.record_voice_session(
        _require_db_pool(),
        guild_id=guild_id,
        voice_channel_id=voice_channel_id,
        text_channel_id=text_channel_id,
    )


async def forget_voice_session(guild_id: int) -> None:
    """voice-session feature の行を 1 件削除する。"""
    await voice_sessions_infrastructure.forget_voice_session(
        _require_db_pool(), guild_id=guild_id
    )


async def load_voice_sessions() -> list[tuple[int, int, int]]:
    """voice-session feature の行をすべて読み込む。"""
    return await voice_sessions_infrastructure.load_voice_sessions(_require_db_pool())


async def _reconnect_vc(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """voice-session feature 経由で記録済み VC session に再接続する。"""
    await voice_sessions_discord_adapter.reconnect_vc(
        _runtime_context(), guild_id, voice_channel_id, text_channel_id
    )


async def _safe_forget_voice_session(guild_id: int) -> None:
    """DB error を warning ログに留めつつ VC session を忘れる。"""
    await voice_sessions_discord_adapter.safe_forget_voice_session(
        _runtime_context(), guild_id
    )


async def _safe_record_voice_session(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """DB error を warning ログに留めつつ VC session を記録する。"""
    await voice_sessions_discord_adapter.safe_record_voice_session(
        _runtime_context(), guild_id, voice_channel_id, text_channel_id
    )


def _record_gateway_recoverable_disconnect() -> None:
    """Discord gateway の復旧対象切断を記録する。"""
    voice_sessions_discord_adapter.record_gateway_recoverable_disconnect(
        _runtime_context()
    )


def _record_user_requested_disconnect(guild_id: int) -> None:
    """パネル/コマンドからの意図的な VC 切断を記録する。"""
    voice_sessions_discord_adapter.record_user_requested_disconnect(
        _runtime_context(), guild_id
    )


def _has_recent_gateway_recoverable_disconnect() -> bool:
    """直近に gateway 起因の復旧対象切断があったかを返す。"""
    return voice_sessions_discord_adapter.has_recent_gateway_recoverable_disconnect(
        _runtime_context()
    )


def _has_recent_user_requested_disconnect(guild_id: int) -> bool:
    """直近にユーザー操作で同 guild の VC 切断を要求していたかを返す。"""
    return voice_sessions_discord_adapter.has_recent_user_requested_disconnect(
        _runtime_context(), guild_id
    )


def _schedule_delayed_voice_session_restore() -> None:
    """gateway ready 再発火後の遅延 VC session 復旧を予約する。"""
    voice_sessions_discord_adapter.schedule_delayed_voice_session_restore(
        _runtime_context()
    )


async def _restore_voice_sessions_on_startup() -> None:
    """voice-session feature 経由で記録済み VC session を復旧する。"""
    await voice_sessions_discord_adapter.restore_voice_sessions_on_startup(
        _runtime_context()
    )


def _make_audio_source(audio_data: bytes) -> discord.AudioSource:
    """voice-playback feature 経由で Discord AudioSource を作る。"""
    return voice_playback_feature.make_audio_source(_runtime_context(), audio_data)


def make_playback_audio_source(audio_data: bytes) -> discord.AudioSource:
    """feature adapter 用の公開 playback context wrapper。"""
    # voice_playback の Protocol へ渡す公開名。bot.py 内の既存 `_make_audio_source`
    # はテスト互換として残す。
    return _make_audio_source(audio_data)


# --- voice_synthesis feature 互換 wrapper ---

# 合成まわりはキャッシュ、候補選定、HTTP 呼び出し、失敗バックオフが絡むため、
# 実装は voice_synthesis feature に置く。ここは既存 API 名を保つ facade。
# 新規コードでは feature 側の関数/Protocol を直接読む方が意図を追いやすい。

_SynthCandidate = voice_synthesis_feature.SynthCandidate


async def fetch_speakers() -> None:
    """voice-synthesis feature 経由で話者 metadata を取得する。"""
    await voice_synthesis_feature.fetch_speakers(_runtime_context())


def _has_missing_configured_speaker_engines() -> bool:
    """設定済み TTS engine のうち話者 metadata が未取得のものがあるかを返す。"""
    return voice_synthesis_feature.has_missing_configured_speaker_engines(
        _runtime_context()
    )


async def _refresh_speakers_if_needed() -> None:
    """voice-synthesis feature 経由で話者 metadata を再取得する。"""
    await voice_synthesis_feature.refresh_speakers_if_needed(_runtime_context())


async def _refresh_missing_speakers_if_needed() -> None:
    """未取得の engine がある場合だけ話者 metadata を再取得する。"""
    await voice_synthesis_feature.refresh_missing_speakers_if_needed(_runtime_context())


def _schedule_missing_speaker_refresh() -> None:
    """voice-synthesis feature 経由で話者再取得をバックグラウンド予約する。"""
    voice_synthesis_feature.schedule_missing_speaker_refresh(_runtime_context())


def _synth_cache_key(
    candidate: _SynthCandidate, text: str, settings: VoiceSynthesisSettings
) -> SynthCacheKey:
    """voice-synthesis cache key を組み立てる。"""
    return voice_synthesis_feature.synth_cache_key(candidate, text, settings)


def _lookup_synth_cache(
    candidates: list[_SynthCandidate], text: str, settings: VoiceSynthesisSettings
) -> bytes | None:
    """voice-synthesis の LRU cache を検索する。"""
    return voice_synthesis_feature.lookup_synth_cache(
        _runtime_context(), candidates, text, settings
    )


def _lookup_recent_synth_cache(
    candidates: list[_SynthCandidate], text: str, settings: VoiceSynthesisSettings
) -> bytes | None:
    """voice-synthesis の短期 cache を検索する。"""
    return voice_synthesis_feature.lookup_recent_synth_cache(
        _runtime_context(), candidates, text, settings
    )


def _store_synth_cache(
    primary_key: SynthCacheKey, actual_key: SynthCacheKey, data: bytes
) -> None:
    """合成済みバイト列を voice-synthesis の LRU cache に保存する。"""
    voice_synthesis_feature.store_synth_cache(
        _runtime_context(), primary_key, actual_key, data
    )


def _store_recent_synth_cache(key: SynthCacheKey, data: bytes) -> None:
    """合成済みバイト列を voice-synthesis の短期 cache に保存する。"""
    voice_synthesis_feature.store_recent_synth_cache(_runtime_context(), key, data)


async def _build_synthesis_candidates(
    requested_speaker_id: int,
) -> list[_SynthCandidate]:
    """feature layer 経由で voice-synthesis 候補を組み立てる。"""
    return await voice_synthesis_feature.build_synthesis_candidates(
        _runtime_context(), requested_speaker_id
    )


async def _synthesize_with_candidate(
    engine_url: str,
    real_id: int,
    text: str,
    settings: VoiceSynthesisSettings,
) -> bytes:
    """voice-synthesis infrastructure 経由で TTS engine request を 1 件実行する。"""
    return await voice_synthesis_feature.synthesize_with_candidate(
        _runtime_context(), engine_url, real_id, text, settings
    )


def get_user_settings(guild_id: int, user_id: int) -> VoiceSettings:
    """legacy の guild_id=0 fallback 付きでユーザーの音声設定を返す。"""
    settings = user_settings.get((guild_id, user_id))
    if settings is not None:
        return settings
    # 旧実装では guild_id を持たない user_id 単位設定があった。外部公開後に
    # サーバー別設定へ移行しても、既存 DB 行を読み捨てないための fallback。
    return user_settings.get((0, user_id), VoiceSettings())


async def _try_candidate(
    cand: _SynthCandidate,
    text: str,
    settings: VoiceSynthesisSettings,
    primary_key: SynthCacheKey | None,
) -> bytes:
    """voice-synthesis 候補を 1 件試す。"""
    return await voice_synthesis_feature.try_candidate(
        _runtime_context(), cand, text, settings, primary_key
    )


async def _run_candidates(
    candidates: list[_SynthCandidate],
    text: str,
    settings: VoiceSynthesisSettings,
    primary_key: SynthCacheKey | None,
) -> bytes:
    """voice-synthesis 候補を優先順に実行する。"""
    return await voice_synthesis_feature.run_candidates(
        _runtime_context(), candidates, text, settings, primary_key
    )


async def synthesize(
    text: str, settings: VoiceSynthesisSettings, cache: bool = False
) -> bytes:
    """voice-synthesis feature 経由で text を合成する。"""
    return await voice_synthesis_feature.synthesize(
        _runtime_context(), text, settings, cache
    )


def _internal_tts_api_should_start() -> bool:
    """internal TTS API feature を起動すべきかを返す。"""
    return internal_tts_api_feature.should_start(_runtime_context())


def get_internal_tts_api_runner() -> web.AppRunner | None:
    """adapter 管理 lifecycle 用の internal TTS API runner を返す。"""
    # adapter が private module global を直接触らないようにするための accessor。
    # TypeScript 化時は InternalTtsApiState の getter に置き換える想定。
    return _internal_tts_api_runner


def set_internal_tts_api_runner(runner: web.AppRunner | None) -> None:
    """adapter 管理 lifecycle 用の internal TTS API runner を保存する。"""
    global _internal_tts_api_runner
    # start/stop が別 feature にあるため、runner の所有権をここで明示的に受け渡す。
    _internal_tts_api_runner = runner


def _internal_tts_api_authorized(request: web.Request) -> bool:
    """internal TTS API request を 1 件検証する。"""
    return internal_tts_api_feature.authorized(_runtime_context(), request)


def _prepare_internal_tts_text(text: str, guild_id: int | None) -> str:
    """internal TTS API feature 経由で text を準備する。"""
    return internal_tts_api_feature.prepare_text(_runtime_context(), text, guild_id)


async def start_internal_tts_api() -> None:
    """internal TTS API feature を起動する。"""
    await internal_tts_api_feature.start(_runtime_context())


async def stop_internal_tts_api() -> None:
    """internal TTS API feature を停止する。"""
    await internal_tts_api_feature.stop(_runtime_context())


async def play_next(guild_id: int, vc: discord.VoiceClient):
    """voice-playback feature 経由で次のキュー済み音声を再生する。"""
    await voice_playback_feature.play_next(_runtime_context(), guild_id, vc)


# --- discord_bot UI feature 互換 export ---

# Discord UI の View/Modal クラスは discord.py の decorator/metaclass と密結合している。
# bot.py で再定義せず feature 側から re-export することで、UI 実装の所在を一箇所にする。
# `_...` alias は古いテスト名を壊さないために残しているだけ。

discord_bot_ui.configure(_runtime_context())

DICT_PAGE_SIZE = discord_bot_ui.DICT_PAGE_SIZE
_dict_items_for_page = discord_bot_ui.dict_items_for_page
build_dict_message = discord_bot_ui.build_dict_message
DictDeleteSelect = discord_bot_ui.DictDeleteSelect
DictView = discord_bot_ui.DictView
DictAddModal = discord_bot_ui.DictAddModal
DictDeleteModal = discord_bot_ui.DictDeleteModal
_build_panel_embed = discord_bot_ui.build_panel_embed
_build_voice_settings_embed = discord_bot_ui.build_voice_settings_embed
VoiceSettingsModal = discord_bot_ui.VoiceSettingsModal
VoiceSettingsView = discord_bot_ui.VoiceSettingsView
_characters_for_engine = discord_bot_ui.characters_for_engine
SPEAKER_PAGE_SIZE = discord_bot_ui.SPEAKER_PAGE_SIZE
_page_items = discord_bot_ui.paginate_items
SpeakerEngineSelect = discord_bot_ui.SpeakerEngineSelect
SpeakerCharacterSelect = discord_bot_ui.SpeakerCharacterSelect
SpeakerStyleSelect = discord_bot_ui.SpeakerStyleSelect
_build_speaker_picker_embed = discord_bot_ui.build_speaker_picker_embed
SpeakerPickerView = discord_bot_ui.SpeakerPickerView
SpeakerCharacterView = discord_bot_ui.SpeakerCharacterView
SpeakerStyleView = discord_bot_ui.SpeakerStyleView
SpeakerCharacterPageButton = discord_bot_ui.SpeakerCharacterPageButton
SpeakerStylePageButton = discord_bot_ui.SpeakerStylePageButton
_refresh_panel_message = discord_bot_ui.refresh_panel_message
ControlPanelView = discord_bot_ui.ControlPanelView
_play_voice_sample = discord_bot_ui.play_voice_sample


def register_persistent_views() -> None:
    """再起動後の公開パネル操作を受ける persistent view を一度だけ登録する。"""
    global _persistent_views_registered
    if _persistent_views_registered:
        return
    client.add_view(ControlPanelView())
    _persistent_views_registered = True


# --- イベント・コマンド ---


@client.event
async def on_ready():
    """discord_bot lifecycle feature 経由で Discord ready を処理する。"""
    await discord_bot_lifecycle.on_ready(_runtime_context())


@client.event
async def on_guild_remove(guild: discord.Guild):
    """discord_bot lifecycle feature 経由で guild 離脱を処理する。"""
    await discord_bot_lifecycle.on_guild_remove(_runtime_context(), guild)


def _attachment_category(content_type: str | None) -> str:
    """添付ファイル MIME type の読み上げカテゴリを返す。"""
    return discord_bot_messages.attachment_category(content_type)


def _build_attachment_notice(attachments: Sequence[discord.Attachment]) -> str:
    """Discord 添付ファイル一覧の読み上げ通知文を返す。"""
    return discord_bot_messages.build_attachment_notice(attachments)


_VOICEVOX_OFFICIAL_URL = discord_bot_help.VOICEVOX_OFFICIAL_URL
_COEIROINK_OFFICIAL_URL = discord_bot_help.COEIROINK_OFFICIAL_URL
_SHAREVOX_OFFICIAL_URL = discord_bot_help.SHAREVOX_OFFICIAL_URL


def _build_help_embed(prefix: str | None = None) -> discord.Embed:
    """discord_bot feature 経由でコマンド一覧 embed を作る。"""
    return discord_bot_help.build_help_embed(prefix)


@dataclass(frozen=True)
class _InternalInteractionHandler:
    """slash command から外した handler をテストしやすい形で保持する。"""

    callback: Callable[..., Coroutine[Any, Any, None]]


async def _join_callback(interaction: discord.Interaction) -> None:
    """パネルの接続ボタンから使う VC 接続 handler。"""
    await discord_bot_commands.join(_runtime_context(), interaction)


join = _InternalInteractionHandler(_join_callback)


@client.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """voice_sessions feature 経由で VC 更新を処理する。"""
    await voice_sessions_discord_adapter.handle_voice_state_update(
        _runtime_context(), member, before, after
    )


async def _leave_callback(interaction: discord.Interaction) -> None:
    """パネルの切断ボタンから使う VC 切断 handler。"""
    await discord_bot_commands.leave(_runtime_context(), interaction)


leave = _InternalInteractionHandler(_leave_callback)


async def _vc_toggle_callback(interaction: discord.Interaction) -> None:
    """互換テスト用に残す VC 接続切り替え handler。"""
    await discord_bot_commands.vc_toggle(_runtime_context(), interaction)


@tree.command(name="vc", description="VCに接続/切断をトグル")
async def vc_toggle(interaction: discord.Interaction):
    """discord_bot feature 経由で VC 接続を切り替える。"""
    await _vc_toggle_callback(interaction)


async def _skip_callback(interaction: discord.Interaction) -> None:
    """パネルのスキップボタンから使う再生停止 handler。"""
    await discord_bot_commands.skip(_runtime_context(), interaction)


skip = _InternalInteractionHandler(_skip_callback)


@tree.command(name="mute", description="指定ユーザーの読み上げをミュート")
@app_commands.describe(user="ミュートするユーザー")
async def mute_cmd(interaction: discord.Interaction, user: discord.Member):
    """discord_bot feature 経由でユーザーをミュートする。"""
    await discord_bot_commands.mute(_runtime_context(), interaction, user)


@tree.command(name="unmute", description="指定ユーザーのミュートを解除")
@app_commands.describe(user="ミュート解除するユーザー")
async def unmute_cmd(interaction: discord.Interaction, user: discord.Member):
    """discord_bot feature 経由でユーザーのミュートを解除する。"""
    await discord_bot_commands.unmute(_runtime_context(), interaction, user)


@tree.command(name="showmute", description="ミュート中のユーザー一覧")
async def showmute_cmd(interaction: discord.Interaction):
    """discord_bot feature 経由でミュート中ユーザーを一覧する。"""
    await discord_bot_commands.showmute(_runtime_context(), interaction)


async def _speaker_callback(
    interaction: discord.Interaction,
    character: str,
    style: str | None = None,
) -> None:
    """パネル UI の話者選択と同じ設定処理を呼び出す互換 handler。"""
    await discord_bot_commands.speaker(
        _runtime_context(), interaction, character, style
    )


speaker = _InternalInteractionHandler(_speaker_callback)


async def speaker_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """話者キャラクターの autocomplete 候補を返す互換 helper。"""
    return await discord_bot_commands.speaker_char_autocomplete(
        _runtime_context(), interaction, current
    )


async def speaker_style_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """話者スタイルの autocomplete 候補を返す互換 helper。"""
    return await discord_bot_commands.speaker_style_autocomplete(
        _runtime_context(), interaction, current
    )


async def _voice_callback(
    interaction: discord.Interaction,
    speed: float | None = None,
    pitch: float | None = None,
    intonation: float | None = None,
    volume: float | None = None,
) -> None:
    """パネル UI の音声設定と同じ保存処理を呼び出す互換 handler。"""
    await discord_bot_commands.voice(
        _runtime_context(), interaction, speed, pitch, intonation, volume
    )


voice = _InternalInteractionHandler(_voice_callback)


async def _dict_callback(interaction: discord.Interaction) -> None:
    """パネルの辞書ボタンから使う辞書 UI handler。"""
    await discord_bot_commands.dictionary(_runtime_context(), interaction)


dict_cmd = _InternalInteractionHandler(_dict_callback)


@tree.command(name="panel", description="読み上げBotの操作パネルを再投稿")
async def panel_cmd(interaction: discord.Interaction):
    """discord_bot feature 経由で統合操作パネルを再投稿する。"""
    await discord_bot_commands.panel(_runtime_context(), interaction)


async def _status_callback(
    interaction: discord.Interaction,
    private: bool = True,
) -> None:
    """パネルの状態ボタンから使う status handler。"""
    await discord_bot_commands.status(_runtime_context(), interaction, private)


status_cmd = _InternalInteractionHandler(_status_callback)


async def _license_callback(interaction: discord.Interaction) -> None:
    """パネルのライセンスボタンから使う license handler。"""
    await discord_bot_commands.license(_runtime_context(), interaction)


license_cmd = _InternalInteractionHandler(_license_callback)


async def _credit_callback(interaction: discord.Interaction) -> None:
    """ライセンス表示へ統合した credit handler の互換入口。"""
    await discord_bot_commands.credit(_runtime_context(), interaction)


credit_cmd = _InternalInteractionHandler(_credit_callback)


async def _help_callback(interaction: discord.Interaction) -> None:
    """旧 help 導線から統合パネルを表示する互換入口。"""
    await discord_bot_commands.help_command(_runtime_context(), interaction)


help_cmd = _InternalInteractionHandler(_help_callback)


@client.event
async def on_message(message: discord.Message):
    """discord_bot feature 経由で Discord message の読み上げを処理する。"""
    await discord_bot_messages.on_message(_runtime_context(), message)


# Discord ログイン時の 503 等のリトライ上限（指数バックオフ: 5, 10, 20, 40, 80 秒）
MAX_LOGIN_RETRIES = 5
# トークン無効化（再生成・失効・4004認証失敗）時のexit前 sleep。
TOKEN_INVALID_BACKOFF_SECONDS = 300


def _run_single_bot(discord_token: str) -> None:
    """単一 token 起動用の互換 wrapper。"""
    launcher.run_single_bot(_runtime_context(), discord_token)


def _terminate_processes(processes: list[subprocess.Popen[Any]]) -> None:
    """子 process 終了用の互換 wrapper。"""
    launcher.terminate_processes(_runtime_context(), processes)


def _run_multi_bots(discord_tokens: list[str]) -> None:
    """複数 token 監督用の互換 wrapper。"""
    launcher.run_multi_bots(_runtime_context(), discord_tokens)


def _mark_dynamic_runtime_exports() -> None:
    """ctx 経由の feature export を静的解析から見えるようにする。

    複数の feature adapter はこの module を runtime context として受け取り、
    ``ctx._log_event`` や ``ctx._build_status_embed`` などの属性を読む。
    ここで名前を列挙しておくことで、テストや adapter が使う runtime API の形を
    変えずに IDE の false positive を避ける。
    """
    runtime_exports: tuple[object, ...] = (
        migration_runner,
        signal,
        emoji_lib,
        _new_trace_id,
        _log_event,
        _record_recent_error,
        _env_flag,
        _resolve_discord_tokens,
        _spawn_background,
        _new_queue,
        _ensure_queue,
        _cleanup_guild_state,
        _cleanup_guild_playback_state,
        _can_start_playback,
        can_start_playback,
        _is_vc_connected,
        is_voice_client_connected,
        _is_vc_playing,
        _safe_disconnect,
        _as_voice_client,
        _has_active_voice_connection,
        _reset_voice_state,
        _require_guild_interaction,
        _respond,
        _voice_settings_lines,
        _build_license_embed,
        _panel_license_lines,
        _build_status_embed,
        _sync_text_processing_state_to_module,
        _sync_text_processing_state_from_module,
        _rebuild_kaomoji_patterns,
        _rebuild_reading_patterns,
        _dict_patterns,
        _synth_cache,
        _recent_synth_cache,
        _synth_in_flight,
        _candidate_fail_until,
        _prune_candidate_fail_until,
        _user_buckets,
        _rate_limit_try_consume,
        _speaker_refresh_lock,
        _last_speaker_refresh_attempt,
        _require_db_pool,
        _invalidate_dict_cache,
        _is_builtin_duplicate,
        _reconnect_vc,
        _safe_forget_voice_session,
        _safe_record_voice_session,
        _record_gateway_recoverable_disconnect,
        _record_user_requested_disconnect,
        _has_recent_gateway_recoverable_disconnect,
        _has_recent_user_requested_disconnect,
        _schedule_delayed_voice_session_restore,
        _restore_voice_sessions_on_startup,
        _make_audio_source,
        make_playback_audio_source,
        _SynthCandidate,
        _has_missing_configured_speaker_engines,
        _refresh_speakers_if_needed,
        _refresh_missing_speakers_if_needed,
        _schedule_missing_speaker_refresh,
        _synth_cache_key,
        _lookup_synth_cache,
        _lookup_recent_synth_cache,
        _store_synth_cache,
        _store_recent_synth_cache,
        _build_synthesis_candidates,
        _synthesize_with_candidate,
        _try_candidate,
        _run_candidates,
        _internal_tts_api_should_start,
        get_internal_tts_api_runner,
        set_internal_tts_api_runner,
        _internal_tts_api_authorized,
        _prepare_internal_tts_text,
        _dict_items_for_page,
        _build_panel_embed,
        _build_voice_settings_embed,
        _characters_for_engine,
        _page_items,
        _build_speaker_picker_embed,
        _refresh_panel_message,
        register_persistent_views,
        _play_voice_sample,
        _attachment_category,
        _build_attachment_notice,
        _VOICEVOX_OFFICIAL_URL,
        _COEIROINK_OFFICIAL_URL,
        _SHAREVOX_OFFICIAL_URL,
        _build_help_embed,
        _run_single_bot,
        _terminate_processes,
        _run_multi_bots,
    )
    _ = runtime_exports


_mark_dynamic_runtime_exports()


if __name__ == "__main__":
    launcher.main(_runtime_context())
