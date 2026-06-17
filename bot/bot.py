import asyncio
import concurrent.futures
import io
import logging
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata
import wave
from collections import OrderedDict, deque
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from types import FrameType, MappingProxyType
from typing import Any, cast

import aiohttp
import asyncpg
import discord
import migrate as migration_runner
from discord import app_commands, ui
from dotenv import load_dotenv
from kaomoji_builtin import KAOMOJI_DICT as _BUILTIN_KAOMOJI_DICT
from readings_builtin import (
    ENGLISH_WORD_READINGS as _BUILTIN_ENGLISH_WORD_READINGS,
)
from readings_builtin import (
    READING_CORRECTIONS as _BUILTIN_READING_CORRECTIONS,
)
from readings_builtin import to_katakana

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

# 設定（環境変数で切り替え）
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_TOKENS_RAW = os.getenv("DISCORD_TOKENS", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_SPEAKER = int(os.getenv("DEFAULT_SPEAKER_ID", "46"))


def _env_flag(name: str, default: bool = False) -> bool:
    """真偽値の環境変数を解釈する。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_discord_token(token: str) -> str:
    """トークン文字列の表記ゆれを吸収する（空白・囲み引用符）。"""
    normalized = token.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {'"', "'"}
    ):
        normalized = normalized[1:-1].strip()
    return normalized


def _resolve_discord_tokens() -> list[str]:
    """DISCORD_TOKENS / DISCORD_TOKEN から起動対象トークン一覧を作る。"""
    tokens: list[str] = []
    if DISCORD_TOKENS_RAW.strip():
        # 全角カンマ混在を許容
        token_source = DISCORD_TOKENS_RAW.replace("，", ",")
        tokens.extend(
            token for token in re.split(r"[\s,]+", token_source.strip()) if token
        )
    if DISCORD_TOKEN.strip():
        tokens.append(DISCORD_TOKEN.strip())

    unique_tokens: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = _normalize_discord_token(token)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_tokens.append(normalized)
    return unique_tokens


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

# 子プロセスの自動再起動（指数バックオフ + クラッシュループ検出）
BOT_RESTART_BACKOFF_MAX_SECONDS = 60
BOT_CRASH_WINDOW_SECONDS = 300
BOT_CRASH_THRESHOLD = 5
BOT_POLL_INTERVAL_SECONDS = 2


def _compose_profile_enabled(profile: str) -> bool:
    profiles = re.split(r"[,\s]+", os.getenv("COMPOSE_PROFILES", ""))
    return profile in {item for item in profiles if item}


def _engine_url(
    env_name: str,
    default: str = "",
    *,
    profile: str | None = None,
    profile_default: str = "",
) -> str:
    if url := os.getenv(env_name):
        return url
    if profile and _compose_profile_enabled(profile):
        return profile_default
    return default


# 各エンジンの定義（名前, URL, IDオフセット）
# IDオフセットでエンジン間のスピーカーID衝突を回避
ENGINES: list[tuple[str, str, int]] = [
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
ENGINES = [(name, url, offset) for name, url, offset in ENGINES if url]

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

    async def close(self) -> None:
        await close_http_session()
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

# ギルドあたりの再生キュー最大長。スパム時は新規メッセージ側を drop して、
# 「読み上げが何分も遅れて続く」体感遅延を抑える（小さい値ほど追従性が良い）。
QUEUE_MAXLEN = 4

# ギルドごとの再生キューと読み上げ対象チャンネル
queues: dict[int, deque[bytes]] = {}
read_channels: dict[int, int] = {}  # guild_id -> channel_id
play_locks: dict[int, asyncio.Lock] = {}  # guild_id -> 再生開始の競合防止ロック
# ギルド内での「合成 → queue 追加」順序を保証するロック。
# 複数メッセージが同時到着した時、短文が先に合成完了して順序が逆転する race を防ぐ。
# 代償としてギルド内は合成がシリアライズされる（ギルド間は並行）。
synth_order_locks: dict[int, asyncio.Lock] = {}
engine_error_notified_at: dict[int, float] = {}  # guild_id -> monotonic seconds
ENGINE_ERROR_NOTIFY_INTERVAL = 30.0

# 起動時 VC 復旧（_restore_voice_sessions_on_startup）の多重起動防止 + リトライ設定
_vc_reconnect_inflight: set[int] = set()
VC_RECONNECT_MAX_ATTEMPTS = 5
VC_RECONNECT_BACKOFF_BASE_SECONDS = 2
VC_RECONNECT_BACKOFF_MAX_SECONDS = 60

# fire-and-forget タスクの参照保持（CPython の GC で消されないように）
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """create_task しつつ参照を保持し、完了時に自動回収する。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _new_queue() -> deque[bytes]:
    """ギルド用の音声キューを新規作成（maxlen 付き）"""
    return deque(maxlen=QUEUE_MAXLEN)


def _ensure_queue(guild_id: int) -> deque[bytes]:
    """guild_id のキューを取得（無ければ maxlen 付きで作成）"""
    return queues.setdefault(guild_id, _new_queue())


def _synth_order_lock(guild_id: int) -> asyncio.Lock:
    """ギルド内での合成→queue追加の順序を保証するロック"""
    return synth_order_locks.setdefault(guild_id, asyncio.Lock())


def _cleanup_guild_state(guild_id: int) -> None:
    """ギルドごとの再生状態（キュー・読み上げch・ロック・通知タイムスタンプ）を破棄"""
    queues.pop(guild_id, None)
    read_channels.pop(guild_id, None)
    play_locks.pop(guild_id, None)
    synth_order_locks.pop(guild_id, None)
    engine_error_notified_at.pop(guild_id, None)


def _cleanup_guild_playback_state(guild_id: int) -> None:
    """一時的な VC 切断時用の部分クリーンアップ。

    Discord WS 4006 等で discord.py が auto-reconnect する場面で呼ばれる。
    `read_channels` は意図的に保持し、再接続後に on_message が引き続き
    読み上げ対象として認識できるようにする。queue/locks は再生コンテキスト
    なので破棄。
    """
    queues.pop(guild_id, None)
    play_locks.pop(guild_id, None)
    synth_order_locks.pop(guild_id, None)
    engine_error_notified_at.pop(guild_id, None)
    # read_channels は保持


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


def _is_vc_connected(vc: discord.VoiceClient) -> bool:
    """VC が接続中かを安全に判定する。

    遷移中の `discord.ClientException` を含め、判定中に何らかの例外が出た場合は
    呼び出し側のコマンドハンドラを巻き添えにしないよう「未接続」として扱う。
    """
    try:
        return vc.is_connected()
    except Exception:
        return False


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
# global_id -> (engine_url, real_speaker_id)
speaker_engine: dict[int, tuple[str, int]] = {}
# キャラクター名 -> [(global_id, スタイル名)]
characters: dict[str, list[tuple[int, str]]] = {}
guild_dicts: dict[int, dict[str, str]] = {}
guild_mutes: dict[int, set[int]] = {}  # guild_id -> set of muted user_ids
_speaker_fetch_success_engines: set[str] = set()

# テキスト前処理用の正規表現（1パスで URL / メール / カスタム絵文字を置換）
_CLEAN_TEXT_PATTERN = re.compile(
    r"(?P<url>https?://\S+)"
    r"|(?P<email>[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"
    r"|<a?:(?P<emoji>\w+):\d+>"
)
# 既存 import 互換のため個別パターンも残す
URL_PATTERN = re.compile(r"https?://\S+")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):\d+>")
MAX_READ_LENGTH = 100


def _clean_text_replace(m: re.Match[str]) -> str:
    if m.group("url") is not None:
        return "URLしょうりゃく"
    if m.group("email") is not None:
        return "メールアドレスしょうりゃく"
    return m.group("emoji")  # カスタム絵文字は名前だけ残す


# kaomoji.json に含まれない素朴な基本形を補完する curated dict。
# Wikipedia の顔文字ページや一般的な日本語利用で頻出するものを中心に選定。
_BASIC_KAOMOJI: dict[str, str] = {
    # 笑顔・喜び
    "(^_^)": "にっこり",
    "(^-^)": "にっこり",
    "(^o^)": "わーい",
    "(^○^)": "にっこり",
    "(^∇^)": "にっこり",
    "(^∀^)": "にっこり",
    "(^ω^)": "にっこり",
    "(*^_^*)": "にこにこ",
    "(*^-^*)": "にこにこ",
    "(*^o^*)": "わーい",
    "(#^^#)": "にこにこ",
    "(*´ω`*)": "にこにこ",
    "(*´∀`*)": "にこにこ",
    "(´∀`)": "にっこり",
    "(＾ω＾)": "にっこり",
    "(・∀・)": "にっこり",
    "＼(^o^)／": "ばんざい",
    "٩(ˊᗜˋ*)و": "わーい",
    # 困惑・汗
    "(^_^;)": "あせ",
    "(^^;)": "あせ",
    "(´∀`;)": "あせ",
    "(;・∀・)": "あせあせ",
    "(;´Д`)": "あせあせ",
    # 悲しい・泣く
    "(;_;)": "なく",
    "(T_T)": "なく",
    "(T-T)": "なく",
    "(ToT)": "なく",
    "(ToT)/~~~": "なく",
    "(´;ω;`)": "なく",
    "( ;∀;)": "なく",
    "(´Д⊂ヽ": "なく",
    "(>_<)": "つらい",
    "(つд⊂)": "なく",
    "(´；ω；｀)": "なく",
    "(；ω；)": "なく",
    # 驚き
    "Σ(ﾟдﾟ)": "びっくり",
    "(ﾟДﾟ)": "びっくり",
    "(ﾟдﾟ)": "びっくり",
    "Σ(°Д°;)": "びっくり",
    "Σ(´∀`;)": "びっくり",
    "(@_@)": "くらくら",
    "(*_*)": "くらくら",
    # 怒り
    "(#゚Д゚)": "おこる",
    "(-_-#)": "おこる",
    "(ಠ_ಠ)": "じとー",
    "(｀・ω・´)": "しゃきーん",
    "(｀・∀・´)": "しゃきーん",
    "(๑•̀ㅂ•́)و✧": "がんばる",
    # お辞儀・謝る
    "m(_ _)m": "ぺこり",
    "m(__)m": "ぺこり",
    "m(。_。)m": "ぺこり",
    # 眠い・ぼー
    "(ー_ー)": "じとー",
    "(-_-)": "うーん",
    "( ˘ω˘)": "ねむい",
    "(_ _)": "ねむい",
    "zzz": "ねむい",
    # 落ち込む・困る
    "(´・ω・`)": "しょぼーん",
    "( ´・ω・`)": "うーん",
    "(´・ω・)": "しょぼーん",
    "(・ω・)": "しょぼーん",
    "(*ﾉωﾉ)": "てれ",
    "( ˙꒳˙ )": "ふむ",
    "(´_ゝ`)": "ふーん",
    "(´Д`)": "はぁ",
    "¯\\_(ツ)_/¯": "やれやれ",
    "orz": "がっくり",
    "OTZ": "がっくり",
    "OTL": "がっくり",
    # 笑い（ネットスラング）
    "(笑)": "わらい",
    "(爆)": "ばくしょう",
    "(苦笑)": "にがわらい",
    # 挨拶・手振り
    "(^_^)/": "ばいばい",
    "ノシ": "ばいばい",
    "(´◡`)": "にっこり",
}

# 横向き（Western）顔文字は「語中誤爆」を避けるため境界付きで別処理する。
_WESTERN_EMOTICON_READING: dict[str, str] = {
    ":-)": "にっこり",
    ":)": "にっこり",
    ":-D": "えがお",
    ":D": "えがお",
    ";-)": "ういんく",
    ";)": "ういんく",
    ":-P": "てへぺろ",
    ":P": "てへぺろ",
    ":-(": "しょんぼり",
    ":(": "しょんぼり",
    ":'-(": "なく",
    ":'(": "なく",
    "XD": "おおわらい",
    "xD": "おおわらい",
    "<3": "はーと",
}
_WESTERN_EMOTICON_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<emo>"
    + "|".join(
        re.escape(k) for k in sorted(_WESTERN_EMOTICON_READING, key=len, reverse=True)
    )
    + r")"
    r"(?![A-Za-z0-9])"
)


# 顔文字辞書（ビルドイン）。
# もともと kaomoji.json で管理していた辞書をコード内に同梱し、
# 起動時ファイルI/Oをなくしてデプロイ環境依存を減らす。
# `_BUILTIN_KAOMOJI_DICT` を直接借用してコピーを増やさない
# （3,000+ 件の二重保持を避ける）。以後 `_KAOMOJI_DICT` がただ一つの実体。
_KAOMOJI_DICT: dict[str, str] = _BUILTIN_KAOMOJI_DICT
# curated な基本形を優先（上書き可能）。ビルドイン辞書側の long-form と
# 重複しにくいシンプル形を中心に登録している。
_KAOMOJI_DICT.update(_BASIC_KAOMOJI)

_KAOMOJI_CHAR_NORMALIZE_MAP = str.maketrans(
    {
        "•": "・",
        "∙": "・",
        "·": "・",
        "⋅": "・",
        "˙": "・",
        "˘": "・",
        "〜": "~",
        "～": "~",
        "―": "-",
        "‐": "-",
        "−": "-",
    }
)


def _normalize_kaomoji_for_lookup(text: str) -> str:
    """顔文字照合用に表記ゆれ（全半角・類似記号）を正規化する。"""
    return unicodedata.normalize("NFKC", text).translate(_KAOMOJI_CHAR_NORMALIZE_MAP)


_KAOMOJI_NORMALIZED_DICT: dict[str, str] = {}
_KAOMOJI_NORMALIZED_MAX_LEN = 0
_KAOMOJI_PATTERN: re.Pattern[str] | None = None


def _rebuild_kaomoji_patterns() -> None:
    """_KAOMOJI_DICT の変更を反映して派生 pattern/normalized を再構築する。

    現状は起動時 1 回しか呼ばれないが、将来 DB 連動で kaomoji を動的追加する時に
    備えて一元化しておく。

    `_KAOMOJI_NORMALIZED_DICT` には「正規化で表記が変わるキー」だけを登録する。
    正規化結果が元キーと同一なエントリは `_replace_kaomoji` で `_KAOMOJI_PATTERN`
    経由で先に拾われるので、別 dict に重複保持する必要がない（数千件分の節約）。
    """
    global _KAOMOJI_NORMALIZED_MAX_LEN, _KAOMOJI_PATTERN

    _KAOMOJI_NORMALIZED_DICT.clear()
    for face, reading in _KAOMOJI_DICT.items():
        normalized = _normalize_kaomoji_for_lookup(face)
        if normalized == face:
            continue
        _KAOMOJI_NORMALIZED_DICT.setdefault(normalized, reading)
    _KAOMOJI_NORMALIZED_MAX_LEN = max(
        (len(k) for k in _KAOMOJI_NORMALIZED_DICT), default=0
    )
    if _KAOMOJI_DICT:
        _KAOMOJI_PATTERN = re.compile(
            "|".join(re.escape(k) for k in sorted(_KAOMOJI_DICT, key=len, reverse=True))
        )
    else:
        _KAOMOJI_PATTERN = None


_rebuild_kaomoji_patterns()
if _KAOMOJI_DICT:
    logger.info(f"顔文字辞書を読み込みました: {len(_KAOMOJI_DICT)}件")
_KAOMOJI_OPENERS = {"(", "（"}
_KAOMOJI_CLOSERS = {")", "）"}
_WESTERN_EMOTICON_TRIGGER_CHARS = {":", ";", "=", "8", "x", "X"}
_JP_NET_SLANG_TRIGGER_CHARS = {"w", "W", "ｗ", "Ｗ"}


def _contains_any_char(text: str, candidates: set[str]) -> bool:
    """text 内に候補文字のいずれかが含まれるかを返す。"""
    return any(ch in candidates for ch in text)


def _replace_kaomoji(text: str) -> str:
    """顔文字を annotation（読み仮名）に置換する。辞書が無ければそのまま返す。"""
    if _KAOMOJI_PATTERN is None:
        return text
    return _KAOMOJI_PATTERN.sub(lambda m: _KAOMOJI_DICT[m.group(0)], text)


def _replace_kaomoji_variant(text: str) -> str:
    """顔文字の表記ゆれ（全半角・類似記号）を吸収して置換する。

    入れ子括弧や複合顔文字も拾えるよう、開き括弧位置から最長一致で探索する。
    """
    if _KAOMOJI_NORMALIZED_MAX_LEN <= 0:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] not in _KAOMOJI_OPENERS:
            out.append(text[i])
            i += 1
            continue

        matched = False
        max_end = min(n, i + _KAOMOJI_NORMALIZED_MAX_LEN)
        for end in range(max_end, i + 1, -1):
            if text[end - 1] not in _KAOMOJI_CLOSERS:
                continue
            token = text[i:end]
            normalized = _normalize_kaomoji_for_lookup(token)
            reading = _KAOMOJI_NORMALIZED_DICT.get(normalized)
            if reading is None:
                continue
            out.append(reading)
            i = end
            matched = True
            break

        if not matched:
            out.append(text[i])
            i += 1

    return "".join(out)


def _replace_western_emoticon(text: str) -> str:
    """横向き顔文字を境界付きで置換する（語中の :D などは誤変換しない）。"""
    return _WESTERN_EMOTICON_PATTERN.sub(
        lambda m: _WESTERN_EMOTICON_READING[m.group("emo")], text
    )


# 日本語圏のネットスラング（笑い表現）。
# 「草」は文字としての意味（くさ）も多いため変換対象にせず、TTS の自然読み「くさ」に
# 任せる。曖昧性のない 2 文字以上の連続だけを置換する。半角/全角・大小文字すべて。
_JP_NET_SLANG_PATTERN = re.compile(r"(?<![A-Za-z0-9.])[wWｗＷ]{2,}(?![A-Za-z0-9.])")


def _replace_jp_net_slang(text: str) -> str:
    """日本語ネットスラング（www / ｗｗ / WWW / Ｗｗ など）を読み仮名に置換する。

    `w` 1 文字を `わら` に対応させ、`www` → `わらわらわら` のように回数を
    保存して読み上げる。
    """
    return _JP_NET_SLANG_PATTERN.sub(lambda m: "わら" * len(m.group(0)), text)


# 固定語のネットスラング辞書。ASCII キーは大文字小文字を区別せず、
# 単語境界 (`(?<![A-Za-z0-9])`/`(?![A-Za-z0-9])`) で英文中の偶然一致を避ける。
# 日本語混在キー (今北産業 等) は単語境界不要。
# キーはすべて小文字で保持し、置換時は match.group(0).lower() で照合する。
_NET_SLANG_DICT: dict[str, str] = {
    # 半角ASCII（大小不問）
    "kwsk": "くわしく",
    "ggrks": "ぐぐれかす",
    "wktk": "わくてか",
    "ktkr": "きたこれ",
    "gdgd": "ぐだぐだ",
    "gkbr": "がくがくぶるぶる",
    "thx": "さんくす",
    "plz": "ぷりーず",
    "pls": "ぷりーず",
    "orz": "がっくり",
    "otz": "がっくり",
    # 日本語/混在
    "今北産業": "いまきたさんぎょう",
    "うp": "アップ",
}

_NET_SLANG_ASCII_KEYS = (
    "kwsk",
    "ggrks",
    "wktk",
    "ktkr",
    "gdgd",
    "gkbr",
    "thx",
    "plz",
    "pls",
    "orz",
    "otz",
)
_NET_SLANG_JA_KEYS = ("今北産業", "うp")

_NET_SLANG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(_NET_SLANG_ASCII_KEYS)
    + r")(?![A-Za-z0-9])|"
    + "|".join(re.escape(k) for k in _NET_SLANG_JA_KEYS),
    re.IGNORECASE,
)


def _replace_net_slang_dict(text: str) -> str:
    """固定語スラングを読み仮名に置換する（kwsk → くわしく, 今北産業 → ... 等）。"""
    return _NET_SLANG_PATTERN.sub(
        lambda m: _NET_SLANG_DICT.get(m.group(0).lower(), m.group(0)),
        text,
    )


# ゲーム/アニメ等、TTS が誤読しがちな英語タイトル/略称の読み辞書。
# `_NET_SLANG_DICT` と同じく単語境界 (`(?<![A-Za-z0-9])`/`(?![A-Za-z0-9])`) で
# 英文中の偶然一致を防ぎ、大小文字どちらでもマッチする。値はカタカナ。
# キーはすべて小文字で保持し、置換時は match.group(0).lower() で照合する。
_TITLE_READING_DICT: dict[str, str] = {
    "fate": "フェイト",
    "fgo": "フェイトグランドオーダー",
    "bleach": "ブリーチ",
    "naruto": "ナルト",
    "pokemon": "ポケモン",
    "dbz": "ドラゴンボールゼット",
    "sao": "ソードアートオンライン",
    "hxh": "ハンターハンター",
    "fps": "エフピーエス",
    "rpg": "アールピージー",
    "mmo": "エムエムオー",
    "trpg": "ティーアールピージー",
    "moba": "モバ",
}

_TITLE_READING_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(_TITLE_READING_DICT) + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _replace_title_readings(text: str) -> str:
    """ゲーム/アニメ等の英語タイトル/略称を読み仮名に置換する。"""
    return _TITLE_READING_PATTERN.sub(
        lambda m: _TITLE_READING_DICT.get(m.group(0).lower(), m.group(0)),
        text,
    )


# 高頻度 Unicode 絵文字の読み替え。必要最小限に絞ってコストを抑える。
_UNICODE_EMOJI_READING: dict[str, str] = {
    "☺️": "にっこり",
    "☺": "にっこり",
    "😀": "にっこり",
    "😁": "にっこり",
    "😂": "わらい",
    "🤣": "わらい",
    "😆": "わらい",
    "😊": "えがお",
    "😍": "はーと",
    "😘": "はーと",
    "🥰": "はーと",
    "😉": "ういんく",
    "🤔": "うーん",
    "😢": "なき",
    "😭": "なき",
    "😡": "おこ",
    "😱": "びっくり",
    "😴": "ねむい",
    "🥺": "うるうる",
    "🙏": "ぺこり",
    "🙇": "ぺこり",
    "👍": "ぐー",
    "👎": "だめ",
    "👏": "ぱちぱち",
    "🙌": "ばんざい",
    "💯": "ひゃくてん",
    "🔥": "あつい",
    "✨": "きらきら",
    "🎉": "おめでとう",
    "❤️": "はーと",
    "❤": "はーと",
    "💕": "はーと",
    "💖": "はーと",
    "💔": "しょっく",
    "💤": "ねむい",
}
_UNICODE_EMOJI_PATTERN: re.Pattern[str] | None = re.compile(
    "|".join(
        re.escape(k) for k in sorted(_UNICODE_EMOJI_READING, key=len, reverse=True)
    )
)
_EMOJI_SKIN_TONE_MODIFIER_PATTERN = re.compile(r"[\U0001F3FB-\U0001F3FF]")

# デコ系でよく使う装飾記号（非Emoji含む）
_DECO_SYMBOL_READING: dict[str, str] = {
    "♡": "はーと",
    "♥": "はーと",
    "❤": "はーと",
    "❣": "はーと",
    "❥": "はーと",
    "☆": "ほし",
    "★": "ほし",
    "✩": "ほし",
    "✪": "ほし",
    "✦": "きらきら",
    "✧": "きらきら",
    "✨": "きらきら",
    "❇": "きらきら",
    "❈": "きらきら",
    "♪": "おんぷ",
    "♫": "おんぷ",
    "♬": "おんぷ",
    "♩": "おんぷ",
    "❀": "はな",
    "✿": "はな",
    "❁": "はな",
    "❃": "はな",
    "❋": "はな",
}
_DECO_SYMBOL_PATTERN = re.compile(
    "|".join(re.escape(k) for k in sorted(_DECO_SYMBOL_READING, key=len, reverse=True))
)
_DECO_SYMBOL_TRIGGER_CHARS = set(_DECO_SYMBOL_READING.keys())

# demojize で得られる short code の代表的なデコ系読み
_EMOJI_SHORTCODE_READING: dict[str, str] = {
    "sparkles": "きらきら",
    "sparkling_heart": "はーと",
    "heart_decoration": "はーと",
    "heart_exclamation": "はーと",
    "two_hearts": "はーと",
    "revolving_hearts": "はーと",
    "growing_heart": "はーと",
    "white_heart": "はーと",
    "pink_heart": "はーと",
    "blue_heart": "はーと",
    "green_heart": "はーと",
    "yellow_heart": "はーと",
    "purple_heart": "はーと",
    "ribbon": "りぼん",
    "gift": "ぷれぜんと",
    "wrapped_gift": "ぷれぜんと",
    "party_popper": "おいわい",
    "confetti_ball": "おいわい",
    "balloon": "ふうせん",
    "cherry_blossom": "さくら",
    "bouquet": "はなたば",
    "musical_note": "おんぷ",
    "musical_notes": "おんぷ",
}

_EMOJI_SHORTCODE_KEYWORD_READING: dict[str, str] = {
    "heart": "はーと",
    "sparkle": "きらきら",
    "star": "ほし",
    "music": "おんぷ",
    "flower": "はな",
    "blossom": "はな",
    "ribbon": "りぼん",
    "gift": "ぷれぜんと",
    "party": "おいわい",
    "confetti": "おいわい",
}


def _replace_deco_symbols(text: str) -> str:
    """デコ記号を読み仮名に置換する。"""
    return _DECO_SYMBOL_PATTERN.sub(lambda m: _DECO_SYMBOL_READING[m.group(0)], text)


def _shortcode_to_reading(shortcode: str) -> str | None:
    """絵文字 short code から当てられる日本語読みを返す。"""
    if not shortcode:
        return None
    direct = _EMOJI_SHORTCODE_READING.get(shortcode)
    if direct is not None:
        return direct
    for keyword, reading in _EMOJI_SHORTCODE_KEYWORD_READING.items():
        if keyword in shortcode:
            return reading
    return None


def _replace_unicode_emoji(text: str) -> str:
    """Unicode絵文字を短い読み仮名へ置換する。未知の絵文字は読まない。"""
    if emoji_lib is not None:
        # クロージャ内では Optional の narrowing を維持できないためローカルに固定する。
        _lib = emoji_lib

        def _replace(chars: str, data: dict[str, Any]) -> str:
            reading = _UNICODE_EMOJI_READING.get(chars)
            if reading is not None:
                return reading
            normalized_chars = _EMOJI_SKIN_TONE_MODIFIER_PATTERN.sub("", chars)
            reading = _UNICODE_EMOJI_READING.get(normalized_chars)
            if reading is not None:
                return reading
            shortcode = _lib.demojize(normalized_chars, delimiters=("", ""))
            shortcode = shortcode.replace(":", "")
            guessed = _shortcode_to_reading(shortcode)
            if guessed is not None:
                return guessed
            return ""

        return _lib.replace_emoji(text, _replace)

    if _UNICODE_EMOJI_PATTERN is None:
        return text
    return _UNICODE_EMOJI_PATTERN.sub(
        lambda m: _UNICODE_EMOJI_READING[m.group(0)], text
    )


def _contains_possible_unicode_emoji(text: str) -> bool:
    """Unicode絵文字らしき文字を含むかを軽量に判定する。"""
    for ch in text:
        cp = ord(ch)
        if ch in _UNICODE_EMOJI_READING:
            return True
        # variation selector-16（絵文字表現の指示）/ ZWJ（絵文字連結）
        if ch in ("\ufe0f", "\u200d"):
            return True
        # Supplementary Symbols / Emoticons / Pictographs / Transport 等
        if 0x1F000 <= cp <= 0x1FAFF:
            return True
        # Misc Symbols / Dingbats
        if 0x2600 <= cp <= 0x27BF:
            return True
        # Enclosed Alphanumeric Supplement（ⓘ 等を含む）
        if 0x1F100 <= cp <= 0x1F1FF:
            return True
        # Regional Indicator Symbols（国旗：🇯🇵 等）
        if 0x1F1E6 <= cp <= 0x1F1FF:
            return True
    return False


def _normalize_emoji_modifiers(text: str) -> str:
    """絵文字肌色修飾子を除去して読み上げノイズを抑える。"""
    return _EMOJI_SKIN_TONE_MODIFIER_PATTERN.sub("", text)


# 誤読されやすい漢字と英単語の built-in 読み補正辞書は readings_builtin.py に分離。
# `/dict` でユーザが登録すると一覧が長くなるので、一般的なものは Bot 側で対応する。
# on_message では apply_dict（ユーザ辞書）の後に適用し、ユーザ辞書で上書き可能にする。
# ランタイムで DB から上書きできるよう、コピーを保持する。
_READING_CORRECTIONS: dict[str, str] = dict(_BUILTIN_READING_CORRECTIONS)
_ENGLISH_WORD_READINGS: dict[str, str] = dict(_BUILTIN_ENGLISH_WORD_READINGS)

# built-in 読み辞書のデフォルトスナップショットへの読み取り専用ビュー。
# DB初期投入やフォールバックで利用する（runtime dict の変更に影響されない）。
# `MappingProxyType` でラップしてあるため、誤って `_DEFAULT_*[k] = v` などの
# 書き込みを行うと TypeError になり、`readings_builtin` 本体への意図しない
# 副作用を防ぐ。dict コピーを増やさないので追加メモリ消費はほぼゼロ。
_DEFAULT_READING_CORRECTIONS: Mapping[str, str] = MappingProxyType(
    _BUILTIN_READING_CORRECTIONS
)
_DEFAULT_ENGLISH_WORD_READINGS: Mapping[str, str] = MappingProxyType(
    _BUILTIN_ENGLISH_WORD_READINGS
)


def _english_base_form(word: str) -> str | None:
    """規則活用の英単語を基底語へ寄せる（簡易）。"""
    if len(word) < 4:
        return None
    # 比較級・最上級: happier → happy / happiest → happy
    if word.endswith("ier") and len(word) >= 5:
        candidate = word[:-3] + "y"
        if candidate in _ENGLISH_WORD_READINGS:
            return candidate
    if word.endswith("iest") and len(word) >= 6:
        candidate = word[:-4] + "y"
        if candidate in _ENGLISH_WORD_READINGS:
            return candidate
    # 一般的な比較級・最上級: bigger → big / biggest → big
    if word.endswith("er") and len(word) >= 4:
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("est") and len(word) >= 5:
        base = word[:-3]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("ies") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith("ied") and len(word) >= 5:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) >= 6:
        base = word[:-3]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if (base + "e") in _ENGLISH_WORD_READINGS:
            return base + "e"
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("ed") and len(word) >= 5:
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
        if (base + "e") in _ENGLISH_WORD_READINGS:
            return base + "e"
        if len(base) >= 2 and base[-1] == base[-2]:
            doubled = base[:-1]
            if doubled in _ENGLISH_WORD_READINGS:
                return doubled
    if word.endswith("es") and len(word) >= 5:
        # washes → wash / goes → go / boxes → box 等
        base = word[:-2]
        if base in _ENGLISH_WORD_READINGS:
            return base
    if word.endswith("s") and len(word) >= 4:
        base = word[:-1]
        if base in _ENGLISH_WORD_READINGS:
            return base
    return None


def _replace_english_word_match(m: re.Match[str]) -> str:
    word = m.group(0)
    key = word.lower()
    reading = _ENGLISH_WORD_READINGS.get(key)
    if reading is not None:
        return reading
    base = _english_base_form(key)
    if base is None:
        return word
    return _ENGLISH_WORD_READINGS.get(base, word)


def _rebuild_reading_patterns() -> None:
    """現在の built-in 読み辞書から正規表現を再構築する。"""
    global _READING_PATTERN, _ENGLISH_WORD_PATTERN
    _READING_PATTERN = (
        re.compile(
            "|".join(
                re.escape(k)
                for k in sorted(_READING_CORRECTIONS, key=len, reverse=True)
            )
        )
        if _READING_CORRECTIONS
        else None
    )
    _ENGLISH_WORD_PATTERN = (
        # 英単語トークン全体を対象にし、callback側で辞書一致/基底語推定を行う。
        # これにより swimming などの活用形も読み補正できる。
        re.compile(r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])", flags=re.IGNORECASE)
        if _ENGLISH_WORD_READINGS
        else None
    )


_ENGLISH_WORD_PATTERN: re.Pattern[str] | None = (
    # 英単語トークン全体を対象にし、callback側で辞書一致/基底語推定を行う。
    # これにより swimming などの活用形も読み補正できる。
    re.compile(r"(?<![A-Za-z])[A-Za-z]+(?![A-Za-z])", flags=re.IGNORECASE)
    if _ENGLISH_WORD_READINGS
    else None
)
_ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")
_rebuild_reading_patterns()


def apply_reading_corrections(text: str) -> str:
    """誤読されやすい漢字を読み仮名に置換する（長一致優先）。"""
    if _READING_PATTERN is not None:
        text = _READING_PATTERN.sub(lambda m: _READING_CORRECTIONS[m.group(0)], text)
    if _ENGLISH_WORD_PATTERN is not None and _ASCII_LETTER_PATTERN.search(text):
        text = _ENGLISH_WORD_PATTERN.sub(_replace_english_word_match, text)
    return text


# DB接続プール
db_pool: asyncpg.Pool | None = None
db_init_lock = asyncio.Lock()

# apply_dict のコンパイル済みパターンキャッシュ（ギルド毎）
_dict_patterns: dict[int, re.Pattern[str]] = {}

# 合成結果の LRU キャッシュ（cache=True でのみ使用）
# 1件あたり最大 ~500KB。max=32 で ~16MB に抑制。挨拶・入退室通知など
# 繰り返し呼ばれる定型文は十分にヒットする。
_synth_cache: OrderedDict[tuple, bytes] = OrderedDict()
_SYNTH_CACHE_MAX = 32
# 短時間の重複合成を抑える TTL キャッシュ（cache=False でも利用）
# 1件あたり最大 ~500KB。max=16 で ~8MB 上限。
_recent_synth_cache: OrderedDict[tuple, tuple[float, bytes]] = OrderedDict()
_RECENT_SYNTH_CACHE_MAX = 16
_RECENT_SYNTH_TTL_SECONDS = float(os.getenv("RECENT_SYNTH_TTL_SECONDS", "45"))
# 同じキーで同時に合成が走らないよう in-flight 管理
_synth_in_flight: dict[tuple, asyncio.Event] = {}
# 失敗した合成候補の短期バックオフ（engine_url, real_id）-> monotonic deadline
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
    tokens = min(
        float(USER_RATE_LIMIT_CAPACITY),
        tokens + elapsed * USER_RATE_LIMIT_REFILL_PER_SEC,
    )
    if tokens < 1.0:
        _user_buckets[key] = (tokens, now)
        return False
    _user_buckets[key] = (tokens - 1.0, now)
    return True


# speaker_engine 空時の再取得を間引く
_speaker_refresh_lock = asyncio.Lock()
_last_speaker_refresh_attempt = 0.0
SPEAKER_REFRESH_INTERVAL = 30.0


def _require_db_pool() -> asyncpg.Pool:
    """db_pool が初期化されているか確認して返す"""
    if db_pool is None:
        raise RuntimeError("DB接続プールが未初期化です（on_ready完了前の可能性）")
    return db_pool


def _invalidate_dict_cache(guild_id: int):
    """指定ギルドの apply_dict パターンキャッシュを破棄"""
    _dict_patterns.pop(guild_id, None)


# --- DB ---


async def init_db():
    """DB接続プールを作成し、テーブルを初期化する（リトライあり）"""
    global db_pool
    if db_pool is not None:
        return

    async with db_init_lock:
        if db_pool is not None:
            return

        for attempt in range(5):
            try:
                # 小規模 Bot 向けに接続数を絞ってメモリ節約（各接続 ~1-2MB）。
                # 複数Bot時は DB_POOL_MAX_SIZE を下げて Postgres 接続数の総量を抑える。
                db_pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=DB_POOL_MIN_SIZE,
                    max_size=DB_POOL_MAX_SIZE,
                )
                break
            except (OSError, asyncpg.PostgresError) as e:
                if attempt < 4:
                    logger.warning(
                        f"DB接続失敗 ({attempt + 1}/5): {e}、2秒後にリトライ"
                    )
                    await asyncio.sleep(2)
                else:
                    raise

        assert db_pool is not None
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    guild_id BIGINT NOT NULL DEFAULT 0,
                    user_id BIGINT NOT NULL,
                    speaker_id INTEGER NOT NULL DEFAULT 46,
                    speed REAL NOT NULL DEFAULT 1.0,
                    pitch REAL NOT NULL DEFAULT 0.0,
                    intonation REAL NOT NULL DEFAULT 1.0,
                    volume REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute(
                "ALTER TABLE user_settings "
                "ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0"
            )

            pk_cols = await conn.fetch(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_name = 'user_settings'
                  AND tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
                """
            )
            current_pk = [row["column_name"] for row in pk_cols]
            if current_pk != ["guild_id", "user_id"]:
                pk_name = await conn.fetchval(
                    """
                    SELECT tc.constraint_name
                    FROM information_schema.table_constraints tc
                    WHERE tc.table_name = 'user_settings'
                      AND tc.constraint_type = 'PRIMARY KEY'
                    """
                )
                if pk_name:
                    escaped_pk_name = pk_name.replace('"', '""')
                    await conn.execute(
                        f'ALTER TABLE user_settings DROP CONSTRAINT "{escaped_pk_name}"'
                    )
                await conn.execute(
                    "ALTER TABLE user_settings ADD PRIMARY KEY (guild_id, user_id)"
                )

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_dicts (
                    guild_id BIGINT NOT NULL,
                    word TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    PRIMARY KEY (guild_id, word)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_mutes (
                    guild_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS builtin_reading_dicts (
                    dict_type TEXT NOT NULL,
                    word TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    PRIMARY KEY (dict_type, word),
                    CHECK (dict_type IN ('jp', 'en'))
                )
            """)
            # 再起動・切断時の VC 復旧用セッション
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_voice_sessions (
                    guild_id BIGINT PRIMARY KEY,
                    voice_channel_id BIGINT NOT NULL,
                    text_channel_id BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        logger.info("DB初期化完了")


async def load_user_settings():
    """DBからユーザー設定をメモリにロード"""
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, user_id, speaker_id, speed, pitch, intonation, volume "
            "FROM user_settings"
        )
    user_settings.clear()
    for row in rows:
        user_settings[(row["guild_id"], row["user_id"])] = VoiceSettings(
            speaker_id=row["speaker_id"],
            speed=row["speed"],
            pitch=row["pitch"],
            intonation=row["intonation"],
            volume=row["volume"],
        )
    logger.info(f"ユーザー設定を読み込みました: {len(user_settings)}件")


async def save_user_setting(guild_id: int, user_id: int, settings: VoiceSettings):
    """ユーザー設定を1件DBに保存"""
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings
                (guild_id, user_id, speaker_id, speed, pitch, intonation, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                speaker_id = $3, speed = $4, pitch = $5, intonation = $6, volume = $7
            """,
            guild_id,
            user_id,
            settings.speaker_id,
            settings.speed,
            settings.pitch,
            settings.intonation,
            settings.volume,
        )


async def load_guild_dicts():
    """DBからギルドの辞書設定をメモリにロード（読みはカタカナ統一）"""
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, word, reading FROM guild_dicts")
    guild_dicts.clear()
    _dict_patterns.clear()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_dicts:
            guild_dicts[gid] = {}
        # ひらがな登録の旧データも実行時にカタカナ化（VOICEVOX is_kana 用）
        guild_dicts[gid][row["word"]] = to_katakana(row["reading"])
    logger.info(f"辞書設定を読み込みました: {len(guild_dicts)}ギルド")


async def load_builtin_reading_dicts():
    """DBから built-in 読み辞書をメモリにロードし、不足するデフォルト語を補完投入する。

    メモリ側は「デフォルト + DB上書き」で構築するため、
    DBが部分投入状態でも built-in の取りこぼしが発生しない。
    """
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT dict_type, word, reading FROM builtin_reading_dicts"
        )

        # 旧 hiragana 投入された値も実行時にカタカナ化（VOICEVOX is_kana 用）
        db_jp = {
            row["word"]: to_katakana(row["reading"])
            for row in rows
            if row["dict_type"] == "jp"
        }
        db_en = {
            row["word"]: to_katakana(row["reading"])
            for row in rows
            if row["dict_type"] == "en"
        }

        # DBにまだ存在しない built-in 項目のみを投入（既存のDB値は保持）。
        missing_seed_rows = [
            ("jp", w, r)
            for w, r in _DEFAULT_READING_CORRECTIONS.items()
            if w not in db_jp
        ] + [
            ("en", w, r)
            for w, r in _DEFAULT_ENGLISH_WORD_READINGS.items()
            if w not in db_en
        ]
        if missing_seed_rows:
            await conn.executemany(
                """
                INSERT INTO builtin_reading_dicts (dict_type, word, reading)
                VALUES ($1, $2, $3)
                ON CONFLICT (dict_type, word) DO NOTHING
                """,
                missing_seed_rows,
            )

    # メモリ辞書はデフォルトを土台に DB で上書きする。
    jp = dict(_DEFAULT_READING_CORRECTIONS)
    jp.update(db_jp)
    en = dict(_DEFAULT_ENGLISH_WORD_READINGS)
    en.update(db_en)

    _READING_CORRECTIONS.clear()
    _READING_CORRECTIONS.update(jp)
    _ENGLISH_WORD_READINGS.clear()
    _ENGLISH_WORD_READINGS.update(en)
    _rebuild_reading_patterns()

    if missing_seed_rows:
        logger.info(
            "built-in読み辞書をDBへ不足分投入しました: "
            f"inserted={len(missing_seed_rows)}件, "
            f"jp={len(_READING_CORRECTIONS)}件, en={len(_ENGLISH_WORD_READINGS)}件"
        )
    else:
        logger.info(
            "built-in読み辞書をDBから読み込みました: "
            f"jp={len(_READING_CORRECTIONS)}件, en={len(_ENGLISH_WORD_READINGS)}件"
        )


def _is_builtin_duplicate(word: str, reading: str) -> bool:
    """user 辞書 (word → reading) がビルドイン辞書と単語+読み完全一致するか判定。

    - 日本語ビルドイン (`_READING_CORRECTIONS`): 単語キーで直接比較
    - 英語ビルドイン (`_ENGLISH_WORD_READINGS`): キーが小文字保持・matching も
      case-insensitive のため、単語側を lowercase 化して比較
    - 読みは大小区別ありで完全一致のみ True（カナ/かなの揺れも別エントリ扱い）

    True なら登録不要（同じ挙動が既にビルドインで実現される）。
    1文字でも違うなら False を返し、ユーザー上書きとして登録可能とする。
    """
    if _READING_CORRECTIONS.get(word) == reading:
        return True
    if _ENGLISH_WORD_READINGS.get(word.lower()) == reading:
        return True
    return False


async def add_dict_entry(guild_id: int, word: str, reading: str) -> bool:
    """辞書エントリをメモリ/DBに保存しパターンキャッシュを無効化する。

    ビルドインと単語+読みが完全一致する場合は登録せず False を返す
    （冗長エントリでDBを汚さないため）。成功時は True。
    読みは VOICEVOX is_kana モードで送るためカタカナ化して保存する。
    """
    # ユーザーがひらがなで入力してもカタカナで保存・適用する
    reading = to_katakana(reading)
    if _is_builtin_duplicate(word, reading):
        return False
    # メモリ更新とキャッシュ無効化を await 前に行い、
    # 他コルーチンから古いパターンで apply_dict される race を防ぐ
    guild_dicts.setdefault(guild_id, {})[word] = reading
    _invalidate_dict_cache(guild_id)
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_dicts (guild_id, word, reading)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, word) DO UPDATE SET reading = $3
            """,
            guild_id,
            word,
            reading,
        )
    return True


async def purge_builtin_duplicates_from_user_dicts() -> int:
    """既存ユーザー辞書からビルドインと完全一致するエントリを一括削除する。

    起動時に1回呼ぶ想定。ビルドイン辞書が後から拡充されてユーザーの古い登録が
    冗長になったケースを掃除する。削除件数を返す。

    DB操作で例外が出てもメモリ削除は確定済みなので、その場では warning ログ
    のみ出して握りつぶす（次起動時に load_guild_dicts→再 purge で自己治癒する）。
    """
    pairs_to_delete: list[tuple[int, str]] = []
    per_guild_count: dict[int, int] = {}
    for gid, d in guild_dicts.items():
        for word, reading in d.items():
            if _is_builtin_duplicate(word, reading):
                pairs_to_delete.append((gid, word))
                per_guild_count[gid] = per_guild_count.get(gid, 0) + 1
    if not pairs_to_delete:
        return 0

    for gid, word in pairs_to_delete:
        d = guild_dicts.get(gid)
        if d:
            d.pop(word, None)
            if not d:
                guild_dicts.pop(gid, None)
    for gid in per_guild_count:
        _invalidate_dict_cache(gid)

    try:
        async with _require_db_pool().acquire() as conn:
            await conn.executemany(
                "DELETE FROM guild_dicts WHERE guild_id = $1 AND word = $2",
                pairs_to_delete,
            )
    except Exception as e:
        # メモリと DB が瞬間的に不整合になるが、次起動時の load_guild_dicts→
        # 再 purge ループで自然に追いつくため、警告ログのみで継続する。
        logger.warning(
            f"ビルドイン重複ユーザー辞書のDB削除に失敗: {e} "
            f"(メモリは {len(pairs_to_delete)} 件削除済み、次起動時に再試行)"
        )
        return len(pairs_to_delete)

    breakdown = ", ".join(
        f"guild={gid}:{n}件" for gid, n in sorted(per_guild_count.items())
    )
    logger.info(
        f"ビルドインと重複するユーザー辞書を {len(pairs_to_delete)} 件削除 "
        f"({breakdown})"
    )
    return len(pairs_to_delete)


async def delete_dict_entry(guild_id: int, word: str):
    """辞書エントリをメモリ/DBから削除しパターンキャッシュを無効化する"""
    d = guild_dicts.get(guild_id)
    if d is not None:
        d.pop(word, None)
        if not d:
            guild_dicts.pop(guild_id, None)
    _invalidate_dict_cache(guild_id)
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_dicts WHERE guild_id = $1 AND word = $2",
            guild_id,
            word,
        )


def apply_dict(guild_id: int, text: str) -> str:
    """テキストに辞書の置換を適用する（1パスで行い連鎖置換を防ぐ）"""
    d = guild_dicts.get(guild_id, {})
    if not d:
        return text
    # コンパイル済みパターンをキャッシュ（長い単語を優先マッチ）
    pattern = _dict_patterns.get(guild_id)
    if pattern is None:
        words_sorted = sorted(d.keys(), key=len, reverse=True)
        pattern = re.compile("|".join(re.escape(w) for w in words_sorted))
        _dict_patterns[guild_id] = pattern
    return pattern.sub(lambda m: d[m.group(0)], text)


async def load_guild_mutes():
    """DBからギルドのミュート設定をメモリにロード"""
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, user_id FROM guild_mutes")
    guild_mutes.clear()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_mutes:
            guild_mutes[gid] = set()
        guild_mutes[gid].add(row["user_id"])
    logger.info(
        f"ミュート設定を読み込みました: {sum(len(v) for v in guild_mutes.values())}件"
    )


async def add_mute(guild_id: int, user_id: int):
    """ミュートを追加"""
    if guild_id not in guild_mutes:
        guild_mutes[guild_id] = set()
    guild_mutes[guild_id].add(user_id)
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            "INSERT INTO guild_mutes (guild_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            guild_id,
            user_id,
        )


async def remove_mute(guild_id: int, user_id: int):
    """ミュートを解除"""
    mutes = guild_mutes.get(guild_id, set())
    mutes.discard(user_id)
    if not mutes:
        guild_mutes.pop(guild_id, None)
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_mutes WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )


def is_muted(guild_id: int, user_id: int) -> bool:
    """ユーザーがミュートされているか"""
    return user_id in guild_mutes.get(guild_id, set())


# --- VC セッション永続化（再起動・切断時の復旧用）---


async def record_voice_session(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """現在の VC 接続状態を DB に保存（UPSERT）。"""
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO active_voice_sessions
                (guild_id, voice_channel_id, text_channel_id, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (guild_id) DO UPDATE SET
                voice_channel_id = EXCLUDED.voice_channel_id,
                text_channel_id = EXCLUDED.text_channel_id,
                updated_at = NOW()
            """,
            guild_id,
            voice_channel_id,
            text_channel_id,
        )


async def forget_voice_session(guild_id: int) -> None:
    """ユーザー意図の切断時に呼び、DB から VC セッションを削除する。"""
    async with _require_db_pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM active_voice_sessions WHERE guild_id = $1",
            guild_id,
        )


async def load_voice_sessions() -> list[tuple[int, int, int]]:
    """全 VC セッションを (guild_id, voice_channel_id, text_channel_id) で返す。"""
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, voice_channel_id, text_channel_id "
            "FROM active_voice_sessions"
        )
    return [
        (row["guild_id"], row["voice_channel_id"], row["text_channel_id"])
        for row in rows
    ]


async def _reconnect_vc(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """元の VC へ再接続する（指数バックオフ・回数制限・多重起動防止）。

    呼び出し元は `_restore_voice_sessions_on_startup` のみ（起動時 VC 復旧）。
    同一 guild の多重起動を防ぐため `_vc_reconnect_inflight` でガード。
    """
    if guild_id in _vc_reconnect_inflight:
        logger.info(f"VC復旧は既に進行中、重複起動をスキップ guild={guild_id}")
        return
    _vc_reconnect_inflight.add(guild_id)
    try:
        guild = client.get_guild(guild_id)
        if guild is None:
            # bot がそのギルドから外れている → 復旧不能
            logger.warning(f"VC復旧失敗（ギルド未参加） guild={guild_id}")
            await forget_voice_session(guild_id)
            return

        channel = guild.get_channel(voice_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            logger.warning(
                f"VC復旧失敗（VCが見つからない） guild={guild_id} "
                f"channel={voice_channel_id}"
            )
            await forget_voice_session(guild_id)
            return

        # 部屋に人がいなければ復帰しない（無音VCで待機しても TTS する相手がいない）
        non_bot_members = [m for m in channel.members if not m.bot]
        if not non_bot_members:
            logger.info(
                f"VC復旧抑止 guild={guild_id} channel={voice_channel_id}: "
                "部屋に人がいないため復帰しません"
            )
            await forget_voice_session(guild_id)
            return

        # 接続/発言権限が剥奪されていれば即諦める（無駄なリトライ防止）
        # 型上は Member (非 Optional) だが、メンバーキャッシュ未読込時に None
        # が返ることがあるため runtime guard は残す。
        me = guild.me
        if me is not None:  # pyright: ignore[reportUnnecessaryComparison]
            perms = channel.permissions_for(me)
            if not perms.connect or not perms.speak:
                logger.warning(
                    f"VC復旧抑止 guild={guild_id} channel={voice_channel_id}: "
                    "接続/発言権限がありません"
                )
                await forget_voice_session(guild_id)
                return

        def _ensure_session_memory() -> None:
            """VC が接続中という前提で session メモリを再反映する。

            起動時 restore は新規プロセスで queues は空のはずだが、念のため
            空 queue で初期化する（前回プロセスの残骸クリーンアップも兼ねる）。
            read_channels は DB の text_channel_id で上書きし、新規 connect
            でも resume でも on_message が読み上げ対象として認識できるよう
            にする（read_channels が None だと on_message が早期 return して
            テキストを読まなくなるバグの修正）。
            """
            queues[guild_id] = _new_queue()
            read_channels[guild_id] = text_channel_id

        for attempt in range(VC_RECONNECT_MAX_ATTEMPTS):
            existing = _as_voice_client(guild.voice_client)
            if existing and _is_vc_connected(existing):
                # discord.py の voice resume 等で voice_client が既に張られて
                # いるケース。connect は不要だがメモリ状態は新規プロセスで
                # 空のため、ここでも必ず再反映する（read_channels が None だと
                # on_message が早期 return してテキストを読まなくなるバグ）。
                _ensure_session_memory()
                # resume 経路では deaf 状態が前プロセスから引き継がれない可能性が
                # あるため、明示的に self_deaf を有効化する。
                try:
                    await guild.change_voice_state(channel=channel, self_deaf=True)
                except Exception as e:
                    logger.warning(f"self_deaf 設定に失敗 guild={guild_id}: {e}")
                logger.info(f"VC既に接続中、メモリ状態を再反映 guild={guild_id}")
                return
            try:
                await channel.connect(self_deaf=True)
                _ensure_session_memory()
                logger.info(f"VC復旧成功 guild={guild_id} channel={voice_channel_id}")
                return
            except Exception as e:
                wait = min(
                    VC_RECONNECT_BACKOFF_BASE_SECONDS * (2**attempt),
                    VC_RECONNECT_BACKOFF_MAX_SECONDS,
                )
                logger.warning(
                    f"VC復旧失敗 ({attempt + 1}/{VC_RECONNECT_MAX_ATTEMPTS}) "
                    f"guild={guild_id}: {e} → {wait}秒後に再試行"
                )
                await asyncio.sleep(wait)

        logger.error(f"VC復旧諦め guild={guild_id}: {VC_RECONNECT_MAX_ATTEMPTS}回失敗")
        await forget_voice_session(guild_id)
    except Exception as e:
        logger.error(f"VC復旧で予期せぬエラー guild={guild_id}: {e}")
    finally:
        _vc_reconnect_inflight.discard(guild_id)


async def _safe_forget_voice_session(guild_id: int) -> None:
    """`forget_voice_session` のラッパー。DB エラーを warning ログのみで握りつぶす。

    on_voice_state_update から `_spawn_background` 経由で呼ばれる想定。
    切断時は即座に DB session を削除する（手動切断/kick の場合は次起動時の
    意図しない rejoin を防ぐ）。一時的なネットワーク断で discord.py が
    auto-reconnect する場合は、再接続側で `_safe_record_voice_session` が
    DB session を再記録するため deploy 復旧は損なわれない。
    """
    try:
        await forget_voice_session(guild_id)
    except Exception as e:
        logger.warning(f"VCセッション削除失敗 guild={guild_id}: {e}")


async def _safe_record_voice_session(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """`record_voice_session` のラッパー。DB エラーを warning ログのみで握りつぶす。

    discord.py の auto-reconnect 成功時に呼ばれ、切断時に消した DB session を
    再記録する。これにより一時的ネットワーク断後に process が落ちても、
    次起動時の `_restore_voice_sessions_on_startup` が VC へ復帰できる。
    """
    try:
        await record_voice_session(guild_id, voice_channel_id, text_channel_id)
    except Exception as e:
        logger.warning(f"VCセッション再保存失敗 guild={guild_id}: {e}")


async def _restore_voice_sessions_on_startup() -> None:
    """起動時に DB から全 VC セッションを順次復旧する（並列度1で rate limit 安全）。

    既知の trade-off:
    /leave 等のユーザー意図切断時に DB の forget が一過性障害で失敗し、
    その後プロセス再起動 + 部屋に人が残存している場合は再起動後にこの restore で
    rejoin してしまう。発生条件が3つ揃う必要があり実害は小さいため、メモリ guard 等
    の追加複雑性は入れず受容している（ユーザーが再度 /leave すれば抜ける）。
    """
    try:
        sessions = await load_voice_sessions()
    except Exception as e:
        logger.warning(f"起動時 VC セッション読み込み失敗: {e}")
        return
    if not sessions:
        return
    logger.info(f"VC復旧を開始: {len(sessions)}件")
    for guild_id, vc_id, tc_id in sessions:
        await _reconnect_vc(guild_id, vc_id, tc_id)


def clean_text(text: str) -> str:
    """読み上げ用にテキストを前処理する。
    顔文字（長一致優先）→ 横向き顔文字（境界付き）→ 日本語ネットスラング
    → デコ記号 → Unicode絵文字
    → 肌色修飾子正規化 → URL/メール/カスタム絵文字 の順で置換する。"""
    # 顔文字パターンは巨大なので、括弧が無い通常文では regex 走査を避ける。
    # 呼び出し順序は `_replace_kaomoji` → `_replace_kaomoji_variant` で固定。
    # `_KAOMOJI_NORMALIZED_DICT` は「正規化で表記が変わるキー」のみ保持する縮小
    # 版なので、生キー（normalized==face）の置換は前段の `_replace_kaomoji` に
    # 依存している。順序を入れ替えたり片方だけ呼ぶと拾えない顔文字が出る点に注意。
    if _contains_any_char(text, _KAOMOJI_OPENERS):
        text = _replace_kaomoji(text)
        text = _replace_kaomoji_variant(text)
    if _contains_any_char(text, _WESTERN_EMOTICON_TRIGGER_CHARS):
        text = _replace_western_emoticon(text)
    if _contains_any_char(text, _JP_NET_SLANG_TRIGGER_CHARS):
        text = _replace_jp_net_slang(text)
    text = _replace_net_slang_dict(text)
    text = _replace_title_readings(text)
    if _contains_any_char(text, _DECO_SYMBOL_TRIGGER_CHARS):
        text = _replace_deco_symbols(text)
    if _contains_possible_unicode_emoji(text):
        text = _replace_unicode_emoji(text)
    if _EMOJI_SKIN_TONE_MODIFIER_PATTERN.search(text):
        text = _normalize_emoji_modifiers(text)
    return _CLEAN_TEXT_PATTERN.sub(_clean_text_replace, text).strip()


# Discord が要求する PCM フォーマット: 48kHz stereo s16le、20ms = 3840B/frame
_DISCORD_SAMPLE_RATE = 48000
_DISCORD_CHANNELS = 2
_DISCORD_SAMPLE_WIDTH = 2  # 16-bit
_DISCORD_FRAME_SIZE = (
    _DISCORD_SAMPLE_RATE * 20 // 1000 * _DISCORD_CHANNELS * _DISCORD_SAMPLE_WIDTH
)


def _make_audio_source(audio_data: bytes) -> discord.AudioSource:
    """音声バイト列から Discord の AudioSource を作成する。

    Discord 互換フォーマット (48kHz/stereo/16bit) の WAV なら PCMAudio を返し、
    ffmpeg のサブプロセス起動コストを省略する。想定と異なる場合は
    FFmpegPCMAudio にフォールバックして互換性を保つ。
    """
    try:
        with wave.open(io.BytesIO(audio_data), "rb") as w:
            if (
                w.getnchannels() == _DISCORD_CHANNELS
                and w.getsampwidth() == _DISCORD_SAMPLE_WIDTH
                and w.getframerate() == _DISCORD_SAMPLE_RATE
            ):
                pcm = w.readframes(w.getnframes())
                # 末尾の半端フレームはゼロパディングして discord.PCMAudio が
                # 取りこぼさないようにする
                remainder = len(pcm) % _DISCORD_FRAME_SIZE
                if remainder:
                    pcm += b"\x00" * (_DISCORD_FRAME_SIZE - remainder)
                return discord.PCMAudio(io.BytesIO(pcm))
    except (wave.Error, EOFError, ValueError) as e:
        logger.debug(f"PCM直接再生不可、FFmpegにフォールバック: {e}")

    return discord.FFmpegPCMAudio(
        io.BytesIO(audio_data),
        pipe=True,
        before_options="-loglevel error",
        # discord.py の型注釈は IO[bytes] のみだが、subprocess.Popen に丸投げしており
        # subprocess.DEVNULL (= -3) も実行時には正しく解釈される。
        stderr=subprocess.DEVNULL,  # pyright: ignore[reportArgumentType]
    )


# --- TTS エンジン ---


async def fetch_speakers():
    """全エンジンからスピーカー一覧を取得して統合キャッシュ"""
    speakers_cache.clear()
    speaker_engine.clear()
    characters.clear()
    _speaker_fetch_success_engines.clear()

    session = await get_http_session()
    for engine_name, engine_url, offset in ENGINES:
        try:
            async with session.get(
                f"{engine_url}/speakers",
                timeout=aiohttp.ClientTimeout(total=TTS_SPEAKERS_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

            count = 0
            for speaker in data:
                char_name = speaker["name"]
                if len(ENGINES) > 1:
                    char_key = f"[{engine_name}] {char_name}"
                else:
                    char_key = char_name
                if char_key not in characters:
                    characters[char_key] = []
                for style in speaker["styles"]:
                    real_id = style["id"]
                    global_id = real_id + offset
                    style_name = style["name"]
                    label = f"{char_key}（{style_name}）"
                    speakers_cache[global_id] = label
                    speaker_engine[global_id] = (
                        engine_url,
                        real_id,
                    )
                    characters[char_key].append((global_id, style_name))
                    count += 1

            _speaker_fetch_success_engines.add(engine_name)
            logger.info(f"スピーカー取得成功: {engine_name} ({count}件)")
        except Exception as e:
            logger.warning(f"スピーカー取得失敗: {engine_name}: {e}")

    logger.info(f"スピーカー一覧合計: {len(speakers_cache)}件")


def _has_missing_configured_speaker_engines() -> bool:
    configured = {name for name, _, _ in ENGINES}
    return bool(configured) and not configured.issubset(_speaker_fetch_success_engines)


async def _refresh_speakers_if_needed() -> None:
    """スピーカー一覧を再取得する（短時間の連打を抑制）。"""
    global _last_speaker_refresh_attempt

    now = time.monotonic()
    if now - _last_speaker_refresh_attempt < SPEAKER_REFRESH_INTERVAL:
        return

    async with _speaker_refresh_lock:
        now = time.monotonic()
        if now - _last_speaker_refresh_attempt < SPEAKER_REFRESH_INTERVAL:
            return
        # fetch_speakers の成否に関わらず試行時刻を記録する。
        # これによりエンジンが継続的にダウンしている場合の
        # SPEAKER_REFRESH_INTERVAL 秒ごとの再試行に抑え、スパムを防ぐ。
        _last_speaker_refresh_attempt = now
        await fetch_speakers()


async def _refresh_missing_speakers_if_needed() -> None:
    """起動が遅れた任意エンジンがあれば、スピーカー一覧を再取得する。"""
    if _has_missing_configured_speaker_engines():
        await _refresh_speakers_if_needed()


def _schedule_missing_speaker_refresh() -> None:
    """autocomplete を待たせないため、不足エンジンの再取得を裏で走らせる。"""
    if _has_missing_configured_speaker_engines():
        _spawn_background(_refresh_speakers_if_needed())


@dataclass(frozen=True)
class _SynthCandidate:
    """音声合成候補。(エンジンURL, 実speakerID, 選ばれた理由)"""

    engine_url: str
    real_id: int
    reason: str


def _synth_cache_key(
    candidate: _SynthCandidate, text: str, settings: VoiceSettings
) -> tuple:
    """合成結果キャッシュのキーを作る"""
    return (
        candidate.engine_url,
        candidate.real_id,
        text,
        settings.speed,
        settings.pitch,
        settings.intonation,
        settings.volume,
        settings.speaker_id,
    )


def _lookup_synth_cache(
    candidates: list[_SynthCandidate], text: str, settings: VoiceSettings
) -> bytes | None:
    """候補のいずれかでキャッシュヒットすれば返す（LRU の最新に昇格）"""
    for cand in candidates:
        key = _synth_cache_key(cand, text, settings)
        cached = _synth_cache.get(key)
        if cached is not None:
            _synth_cache.move_to_end(key)
            return cached
    return None


def _lookup_recent_synth_cache(
    candidates: list[_SynthCandidate], text: str, settings: VoiceSettings
) -> bytes | None:
    """短時間キャッシュを参照して、期限内ヒットがあれば返す。"""
    now = time.monotonic()
    for cand in candidates:
        key = _synth_cache_key(cand, text, settings)
        entry = _recent_synth_cache.get(key)
        if entry is None:
            continue
        expires_at, data = entry
        if expires_at <= now:
            _recent_synth_cache.pop(key, None)
            continue
        _recent_synth_cache.move_to_end(key)
        return data
    return None


def _store_synth_cache(primary_key: tuple, actual_key: tuple, data: bytes) -> None:
    """合成結果をキャッシュ。primary と実候補が異なる場合は両方のキーで保存する"""
    _synth_cache[actual_key] = data
    _synth_cache.move_to_end(actual_key)
    if actual_key != primary_key:
        _synth_cache[primary_key] = data
        _synth_cache.move_to_end(primary_key)
    while len(_synth_cache) > _SYNTH_CACHE_MAX:
        _synth_cache.popitem(last=False)


def _store_recent_synth_cache(key: tuple, data: bytes) -> None:
    """短時間キャッシュへ保存する（TTL + LRU）。"""
    _recent_synth_cache[key] = (time.monotonic() + _RECENT_SYNTH_TTL_SECONDS, data)
    _recent_synth_cache.move_to_end(key)
    while len(_recent_synth_cache) > _RECENT_SYNTH_CACHE_MAX:
        _recent_synth_cache.popitem(last=False)


async def _build_synthesis_candidates(
    requested_speaker_id: int,
) -> list[_SynthCandidate]:
    """音声合成の候補エンジンを優先順で構築する（重複排除付き）。"""
    seen: set[tuple[str, int]] = set()
    candidates: list[_SynthCandidate] = []

    def add(engine_url: str, real_id: int, reason: str) -> None:
        key = (engine_url, real_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append(_SynthCandidate(engine_url, real_id, reason))

    # 1. 要求された speaker_id のマッピング
    if (info := speaker_engine.get(requested_speaker_id)) is not None:
        add(info[0], info[1], "requested_speaker")

    # 2. speaker_engine が空なら再取得してから requested_speaker_id を再確認
    if not speaker_engine:
        await _refresh_speakers_if_needed()
        if (info := speaker_engine.get(requested_speaker_id)) is not None:
            add(info[0], info[1], "requested_after_refresh")

    # 3. DEFAULT_SPEAKER のマッピング
    if (info := speaker_engine.get(DEFAULT_SPEAKER)) is not None:
        add(info[0], info[1], "default_speaker_mapping")

    # 4. キャッシュに存在する speaker_id の小さい順に 3 件
    for global_id in sorted(speaker_engine.keys())[:3]:
        info = speaker_engine[global_id]
        add(info[0], info[1], "cached_speaker_fallback")

    # 5. 最終手段: 各エンジンへ DEFAULT_SPEAKER を直接投げる。
    # 注意: DEFAULT_SPEAKER は VOICEVOX のデフォルト値（46=小夜/SAYO ノーマル）が前提。
    # COEIROINK/SHAREVOX 単体運用の場合は DEFAULT_SPEAKER_ID を適切に設定すること。
    for _, engine_url, _ in ENGINES:
        add(engine_url, DEFAULT_SPEAKER, "raw_default_id")

    return candidates


async def _synthesize_with_candidate(
    engine_url: str,
    real_id: int,
    text: str,
    settings: VoiceSettings,
) -> bytes:
    """指定候補で音声合成を1回実行する。

    辞書値はカタカナで保存されているため、apply_reading_corrections 適用後の
    text はカタカナ部分が多く、OpenJTalk が漢字を再解析する余地が小さい。
    （`is_kana=true` モードは AquesTalk 表記＋アクセント記号必須で本番投入できず）
    """
    session = await get_http_session()
    params = {"text": text, "speaker": real_id}
    async with session.post(
        f"{engine_url}/audio_query",
        params=params,
        timeout=aiohttp.ClientTimeout(total=TTS_AUDIO_QUERY_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        query = await resp.json()
    # ユーザーの音声パラメータを適用
    query["speedScale"] = settings.speed
    query["pitchScale"] = settings.pitch
    query["intonationScale"] = settings.intonation
    query["volumeScale"] = settings.volume
    # Discord 互換フォーマットを直接要求 → ffmpeg 経由の変換を省略可能にする
    # 未対応エンジンは無視するのでデフォルトフォーマットで返る（FFmpeg で再生）
    query["outputSamplingRate"] = _DISCORD_SAMPLE_RATE
    query["outputStereo"] = True

    async with session.post(
        f"{engine_url}/synthesis",
        params={"speaker": real_id},
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=TTS_SYNTHESIS_TIMEOUT_SECONDS),
    ) as resp:
        resp.raise_for_status()
        return await resp.read()


def get_user_settings(guild_id: int, user_id: int) -> VoiceSettings:
    """ユーザーの音声設定を返す"""
    settings = user_settings.get((guild_id, user_id))
    if settings is not None:
        return settings
    # 旧スキーマから移行した guild_id=0 の設定を後方互換として参照
    return user_settings.get((0, user_id), VoiceSettings())


async def _try_candidate(
    cand: _SynthCandidate,
    text: str,
    settings: VoiceSettings,
    primary_key: tuple | None,
) -> bytes:
    """候補を1つ試して成功データを返す。成功時はキャッシュ保存とバックオフ解除も行う。
    失敗時はバックオフを設定し、元の例外を再送出する。"""
    pair = (cand.engine_url, cand.real_id)
    try:
        data = await _synthesize_with_candidate(
            cand.engine_url, cand.real_id, text, settings
        )
    except (aiohttp.ClientError, TimeoutError):
        _candidate_fail_until[pair] = time.monotonic() + CANDIDATE_FAIL_BACKOFF_SECONDS
        raise

    if _candidate_fail_until.pop(pair, None) is not None:
        logger.info(
            f"音声合成エンジン復旧: engine={cand.engine_url}, speaker={cand.real_id}"
        )
    actual_key = _synth_cache_key(cand, text, settings)
    _store_recent_synth_cache(actual_key, data)
    if primary_key is not None and actual_key != primary_key:
        _store_recent_synth_cache(primary_key, data)
    if primary_key is not None:
        _store_synth_cache(primary_key, actual_key, data)
    return data


async def _run_candidates(
    candidates: list[_SynthCandidate],
    text: str,
    settings: VoiceSettings,
    primary_key: tuple | None,
) -> bytes:
    """候補を順に試して最初に成功したものを返す。全滅時は例外を送出する。"""
    _prune_candidate_fail_until()  # 期限切れ entry を定期的に掃除
    last_error: Exception | None = None
    attempted = False
    now = time.monotonic()

    for idx, cand in enumerate(candidates):
        if _candidate_fail_until.get((cand.engine_url, cand.real_id), 0.0) > now:
            continue  # バックオフ中はスキップ
        attempted = True
        try:
            data = await _try_candidate(cand, text, settings, primary_key)
            if idx > 0:
                logger.warning(
                    f"音声合成フォールバック成功: reason={cand.reason}, "
                    f"engine={cand.engine_url}, speaker={cand.real_id}"
                )
            return data
        except (aiohttp.ClientError, TimeoutError) as e:
            last_error = e
            logger.warning(
                f"音声合成候補失敗: reason={cand.reason}, engine={cand.engine_url}, "
                f"speaker={cand.real_id}, error={e}"
            )

    # 全候補がバックオフ中で 1 つも試行しなかった場合は、
    # 追加のネットワークアクセスをせず即時失敗として返す。
    # （障害時の過密リトライ抑制）
    if not attempted:
        raise aiohttp.ClientConnectionError("音声合成候補はバックオフ中です")

    if last_error is not None:
        raise last_error
    raise RuntimeError("音声合成に失敗しました（全フォールバック候補失敗）")


async def synthesize(text: str, settings: VoiceSettings, cache: bool = False) -> bytes:
    """エンジンでテキストを音声合成して wav バイトを返す。

    cache=True の時のみ結果を LRU キャッシュする。挨拶や入退室通知など
    繰り返し発声される定型文に対して指定する。
    """
    if not ENGINES:
        raise RuntimeError("TTSエンジンが設定されていません")

    candidates = await _build_synthesis_candidates(settings.speaker_id)
    if not candidates:
        raise RuntimeError("音声合成候補がありません。エンジン設定を確認してください")

    primary_key = _synth_cache_key(candidates[0], text, settings)

    if not cache:
        recent = _lookup_recent_synth_cache(candidates, text, settings)
        if recent is not None:
            return recent

        in_flight = _synth_in_flight.get(primary_key)
        if in_flight is not None:
            await in_flight.wait()
            recent = _lookup_recent_synth_cache(candidates, text, settings)
            if recent is not None:
                return recent
        in_flight_event = asyncio.Event()
        _synth_in_flight[primary_key] = in_flight_event
        try:
            return await _run_candidates(candidates, text, settings, primary_key=None)
        finally:
            _synth_in_flight.pop(primary_key, None)
            in_flight_event.set()

    # キャッシュ確認 → in-flight 待機 → 再度キャッシュ確認 の順で重複HTTPを防ぐ
    cached = _lookup_synth_cache(candidates, text, settings)
    if cached is not None:
        return cached

    in_flight = _synth_in_flight.get(primary_key)
    if in_flight is not None:
        await in_flight.wait()
        cached = _lookup_synth_cache(candidates, text, settings)
        if cached is not None:
            return cached
        # 先行タスクが失敗した → 自分で再合成する

    in_flight_event = asyncio.Event()
    _synth_in_flight[primary_key] = in_flight_event
    try:
        return await _run_candidates(candidates, text, settings, primary_key)
    finally:
        _synth_in_flight.pop(primary_key, None)
        in_flight_event.set()


async def play_next(guild_id: int, vc: discord.VoiceClient):
    """キューから次の音声を再生する"""
    lock = play_locks.setdefault(guild_id, asyncio.Lock())
    async with lock:
        # 既に切断済み・再生中・一時停止中なら何もしない
        if not _can_start_playback(vc):
            return

        queue = queues.get(guild_id)
        if not queue:
            return

        audio_data = queue.popleft()
        source = _make_audio_source(audio_data)

        def after_play(error: Exception | None) -> None:
            if error:
                logger.error(f"再生エラー: {error}")
            future = asyncio.run_coroutine_threadsafe(
                play_next(guild_id, vc), client.loop
            )

            def _log_future_exception(fut: concurrent.futures.Future[None]) -> None:
                try:
                    fut.result()
                except Exception as exc:
                    logger.error(f"次の再生でエラー: {exc}")

            future.add_done_callback(_log_future_exception)

        try:
            vc.play(source, after=after_play)
        except discord.ClientException as e:
            # VC が生きていれば一過性の可能性があるのでキューに積み直す。
            # 切断済みなら next play_next を発火させる経路が無いので破棄。
            if _is_vc_connected(vc):
                logger.warning(f"再生失敗、音声をキュー先頭に戻す: {e}")
                queue.appendleft(audio_data)
            else:
                logger.warning(f"再生スキップ（VC切断済み）、音声を破棄: {e}")


# --- 辞書UI ---


def build_dict_message(guild_id: int) -> tuple[str, discord.ui.View]:
    """辞書一覧のメッセージとボタンViewを生成する"""
    d = guild_dicts.get(guild_id, {})
    if d:
        lines = [f"  {word} → {reading}" for word, reading in d.items()]
        content = f"辞書設定（{len(d)}件登録済み）\n" + "\n".join(lines)
    else:
        content = "辞書設定（登録なし）"
    return content, DictView(guild_id)


class DictView(ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    @ui.button(label="追加", style=discord.ButtonStyle.primary)
    async def add_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DictAddModal(self.guild_id))

    @ui.button(label="削除", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DictDeleteModal(self.guild_id))


class DictAddModal(ui.Modal, title="辞書に追加"):
    word = ui.TextInput(label="置換元", placeholder="例: w", max_length=100)
    reading = ui.TextInput(label="読み", placeholder="例: ダブリュー", max_length=200)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        word = self.word.value.strip()
        reading = self.reading.value.strip()
        if not word or not reading:
            await interaction.response.send_message(
                "置換元と読みの両方を入力してください", ephemeral=True
            )
            return

        added = await add_dict_entry(self.guild_id, word, reading)
        if not added:
            await interaction.response.send_message(
                f"「{word} → {reading}」はビルドイン辞書と完全一致するため "
                "登録不要です（読みを変えれば登録可能）",
                ephemeral=True,
            )
            return

        content, view = build_dict_message(self.guild_id)
        await interaction.response.edit_message(content=content, view=view)


class DictDeleteModal(ui.Modal, title="辞書から削除"):
    word = ui.TextInput(label="削除する単語", placeholder="例: w", max_length=100)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        word = self.word.value.strip()
        d = guild_dicts.get(self.guild_id, {})
        if word not in d:
            await interaction.response.send_message(
                f"「{word}」は辞書に登録されていません", ephemeral=True
            )
            return

        await delete_dict_entry(self.guild_id, word)

        content, view = build_dict_message(self.guild_id)
        await interaction.response.edit_message(content=content, view=view)


# --- イベント・コマンド ---


@client.event
async def on_ready():
    global _migrations_ran
    if RUN_DB_MIGRATIONS and not _migrations_ran:
        await migration_runner.run_pending_migrations(DATABASE_URL, logger=logger)
        _migrations_ran = True
    await init_db()
    await load_builtin_reading_dicts()
    await load_user_settings()
    await load_guild_dicts()
    await load_guild_mutes()
    # 起動高速化のため一時無効化:
    # - ビルドイン重複ユーザー辞書の掃除（DB全件走査 + DELETE）
    # try:
    #     await purge_builtin_duplicates_from_user_dicts()
    # except Exception as e:
    #     logger.warning(f"ビルドイン重複ユーザー辞書の掃除でエラー: {e}")
    try:
        await tree.sync()
        logger.info("スラッシュコマンドを同期しました")
    except Exception as e:
        logger.warning(f"スラッシュコマンドの同期に失敗: {e}")
    user = client.user
    if user is not None:
        logger.info(f"Botログイン: {user} (ID: {user.id})")

    try:
        await client.change_presence(activity=discord.CustomActivity(name="読み上げ中"))
    except Exception as e:
        logger.warning(f"プレゼンス設定に失敗: {e}")

    # 起動高速化のため一時無効化:
    # - スピーカー一覧の事前取得（各TTSエンジンへのHTTPアクセス）
    # try:
    #     await fetch_speakers()
    # except Exception as e:
    #     logger.warning(f"スピーカー一覧の取得に失敗しました: {e}")

    # 起動時の VC 復旧（プロセス再起動・デプロイ後の復帰用）
    # fetch_speakers より後にすることで TTS が使えない状態での接続を避ける。
    # background 化して on_ready 自体は即座に return（ゲートウェイ再接続時の
    # on_ready 再発火と長時間 await の重複を避ける）
    # _spawn_background(_restore_voice_sessions_on_startup())


@client.event
async def on_guild_remove(guild: discord.Guild):
    """Bot がギルドから外れた時のメモリ解放。
    DB エントリは他サーバーで再招待される可能性があるため残置するが、
    VC セッションは再招待時の意図しない自動接続を避けるため削除する。"""
    guild_id = guild.id
    try:
        await forget_voice_session(guild_id)
    except Exception as e:
        logger.warning(f"VCセッション削除に失敗: {e}")
    _cleanup_guild_state(guild_id)
    guild_dicts.pop(guild_id, None)
    guild_mutes.pop(guild_id, None)
    _dict_patterns.pop(guild_id, None)
    # このギルドに属する user_settings エントリをメモリから削除
    stale_keys = [k for k in user_settings if k[0] == guild_id]
    for k in stale_keys:
        user_settings.pop(k, None)
    # レートリミットのユーザバケットも同様に解放
    stale_buckets = [k for k in _user_buckets if k[0] == guild_id]
    for k in stale_buckets:
        _user_buckets.pop(k, None)
    logger.info(f"ギルド退出によりメモリ状態を解放 (Guild: {guild_id})")


_VOICEVOX_OFFICIAL_URL = "https://voicevox.hiroshiba.jp/"
_COEIROINK_OFFICIAL_URL = "https://coeiroink.com/"
_SHAREVOX_OFFICIAL_URL = "https://sharevox.app/"


def _attachment_category(content_type: str | None) -> str:
    """添付ファイルの content_type からひらがなのカテゴリ名を返す。

    Discord の Attachment.content_type は MIME タイプ。未知や None の場合は
    汎用的に「ふぁいる」へフォールバックする。
    """
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "がぞう"
    if ct.startswith("video/"):
        return "どうが"
    if ct.startswith("audio/"):
        return "おんせい"
    if ct == "application/pdf":
        return "ぴーでぃーえふ"
    if ct.startswith("text/"):
        return "てきすとふぁいる"
    if ct in (
        "application/zip",
        "application/x-zip-compressed",
        "application/x-7z-compressed",
        "application/x-tar",
        "application/gzip",
    ):
        return "あっしゅくふぁいる"
    return "ふぁいる"


def _build_attachment_notice(attachments: Sequence[discord.Attachment]) -> str:
    """添付ファイル群を「〜がてんぷされました」の読み上げ文に変換する。

    複数カテゴリが混在する場合は「がぞうとどうがが…」のように「と」で連結する。
    添付なしなら空文字を返す。
    """
    seen: list[str] = []
    for att in attachments:
        category = _attachment_category(att.content_type)
        if category not in seen:
            seen.append(category)
    if not seen:
        return ""
    return "と".join(seen) + "がてんぷされました"


def _build_help_embed(prefix: str | None = None) -> discord.Embed:
    """コマンド一覧の Embed を生成する。

    /join と /help で同じ Embed を共有するためのヘルパー。
    `prefix` が指定された場合は description の冒頭に挿入する。
    """
    body = (
        "`/vc` — VCに接続/切断（トグル）\n"
        "`/join` — VCに接続\n"
        "`/leave` — VCから切断\n"
        "`/skip` — 読み上げをスキップ\n"
        "`/speaker` — 読み上げキャラクター変更\n"
        "`/voice` — 話速・音高・抑揚・音量\n"
        "`/dict` — 読み上げ辞書の管理\n"
        "`/mute` — ユーザーをミュート\n"
        "`/unmute` — ミュート解除\n"
        "`/showmute` — ミュート一覧\n"
        "`/help` — このヘルプを表示\n\n"
        "各ボイスおよびライセンスはこちら:\n"
        f"VOICEVOX: {_VOICEVOX_OFFICIAL_URL}\n"
        f"COEIROINK: {_COEIROINK_OFFICIAL_URL}\n"
        f"SHAREVOX: {_SHAREVOX_OFFICIAL_URL}"
    )
    description = f"{prefix}\n\n{body}" if prefix else body
    return discord.Embed(
        title="読み上げBot — コマンド一覧",
        description=description,
        color=0x00B0F4,
    )


@tree.command(name="join", description="ボイスチャンネルに接続")
async def join(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    # スラッシュコマンドはギルド内なら interaction.user は Member だが、
    # 型レベルでは User | Member なので narrow する。
    invoker = cast(discord.Member, interaction.user)
    if invoker.voice is None or invoker.voice.channel is None:
        await interaction.response.send_message("先にボイスチャンネルに入ってください")
        return

    channel = invoker.voice.channel

    text_channel_id = interaction.channel_id
    if text_channel_id is None:
        await interaction.response.send_message(
            "テキストチャンネル情報を取得できませんでした"
        )
        return

    # 型上は Member (非 Optional) だが、メンバーキャッシュ未読込時に None が
    # 返ることがあるため runtime guard は残す。
    me = guild.me
    if me is None and client.user is not None:  # pyright: ignore[reportUnnecessaryComparison]
        me = guild.get_member(client.user.id)
    if me is None:
        await interaction.response.send_message(
            "Botの権限情報を取得できませんでした。しばらく待ってから再試行してください"
        )
        return

    perms = channel.permissions_for(me)
    if not perms.connect:
        await interaction.response.send_message("そのVCに接続する権限がありません")
        return
    if not perms.speak:
        await interaction.response.send_message("そのVCで発言する権限がありません")
        return
    if channel.user_limit and len(channel.members) >= channel.user_limit:
        if not perms.manage_channels:
            await interaction.response.send_message("VCの人数制限に達しています")
            return

    await interaction.response.defer(thinking=True)

    # voice_client が残骸として残っていることがあるため実接続を確認する
    existing_active = _has_active_voice_connection(guild)
    try:
        if existing_active:
            existing_vc = _as_voice_client(guild.voice_client)
            if existing_vc is not None:
                await existing_vc.move_to(channel)
        else:
            # stale な voice_client が残っていれば掃除してから新規接続。
            # self_deaf=True で受信を切り、他人の音声パケット処理コスト (CPU/帯域)
            # を Discord 側で抑制する。送信は維持するため self_mute は付けない。
            await _reset_voice_state(guild)
            await channel.connect(self_deaf=True)
    except Exception as e:
        await interaction.followup.send(f"VCへの接続に失敗しました: {e}")
        return

    if existing_active:
        # move_to の場合: 既存キュー（未再生の音声）は保持
        _ensure_queue(guild.id)
    else:
        # 新規接続: 切断クリーンアップ漏れ等で残存する古いキューを破棄
        queues[guild.id] = _new_queue()
    read_channels[guild.id] = text_channel_id

    # 再起動・切断時に元の VC へ復旧できるようセッションを永続化
    try:
        await record_voice_session(guild.id, channel.id, text_channel_id)
    except Exception as e:
        logger.warning(f"VCセッション保存に失敗: {e}")

    embed = _build_help_embed(
        prefix=(
            f"「{channel.name}」に接続しました\n"
            "このチャンネルのメッセージを読み上げます"
        ),
    )
    await interaction.followup.send(embed=embed)

    # 接続時に音声で挨拶
    try:
        # ※ パフォーマンス調査のため _synth_order_lock を一旦無効化
        # async with _synth_order_lock(guild.id):
        settings = get_user_settings(guild.id, interaction.user.id)
        audio_data = await synthesize("せつぞくしました", settings, cache=True)
        vc = _as_voice_client(guild.voice_client)
        if vc and _is_vc_connected(vc):
            queues[guild.id].append(audio_data)
        vc = _as_voice_client(guild.voice_client)
        if vc and _is_vc_connected(vc) and _can_start_playback(vc):
            await play_next(guild.id, vc)
    except Exception as e:
        logger.error(f"接続挨拶の音声合成エラー: {e}")


@client.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        is_self_event = client.user is not None and member.id == client.user.id
        if is_self_event:
            guild_id = member.guild.id
            if after.channel is None:
                # Bot 自身の切断 → 一時的な切断 (Discord WS 4006 等) に備えて
                # `read_channels` は保持し、queue/locks など再生コンテキストのみ破棄。
                # discord.py の auto-reconnect が成功すれば on_message で読み上げが
                # 継続できる。/leave 等の真の終了では呼び出し側が前後で
                # `_cleanup_guild_state` を呼んで `read_channels` も最終的に削除する
                # ため、ここで保持しても安全（最終状態は全クリア）。
                _cleanup_guild_playback_state(guild_id)
                # 手動切断/kick の場合 discord.py は auto-reconnect しないので、
                # DB session を即座に削除して次起動時の意図しない rejoin を防ぐ。
                # 一時的なネットワーク断で discord.py が auto-reconnect する場合は
                # 下の reconnect パスで `_safe_record_voice_session` が再記録する。
                _spawn_background(_safe_forget_voice_session(guild_id))
                return

            # Bot 自身の再接続 → 残キューがあれば再生再開
            vc = _as_voice_client(member.guild.voice_client)
            queue = queues.get(guild_id)
            if vc and queue and _can_start_playback(vc):
                await play_next(guild_id, vc)

            before_channel_id = getattr(before.channel, "id", None)
            after_channel_id = getattr(after.channel, "id", None)
            self_moved_channels = (
                before.channel is not None
                and after_channel_id is not None
                and before_channel_id != after_channel_id
            )
            if self_moved_channels and vc and _is_vc_connected(vc):
                bot_channel = vc.channel
                non_bot_members = [m for m in bot_channel.members if not m.bot]
                if not non_bot_members:
                    # 管理者の移動などで Bot だけの VC に入った場合は、
                    # session を再記録せずその場で落とす。
                    try:
                        await forget_voice_session(guild_id)
                    except Exception as e:
                        logger.warning(f"VCセッション削除に失敗: {e}")
                    await _safe_disconnect(vc)
                    _cleanup_guild_state(guild_id)
                    logger.info(
                        f"BotのみのVCへ移動されたため自動切断 (Guild: {guild_id})"
                    )
                    return

            # discord.py の auto-reconnect 成功時は、切断時に消した DB session を
            # 再記録する。これによりネットワーク断後にプロセスが落ちても、
            # 次起動の `_restore_voice_sessions_on_startup` で復帰できる。
            # `read_channels` が無い場合 (/leave 後等) は再記録しない。
            # stale voice_client を弾くため `_is_vc_connected` で実接続を確認。
            text_channel_id = read_channels.get(guild_id)
            if vc and _is_vc_connected(vc) and text_channel_id is not None:
                _spawn_background(
                    _safe_record_voice_session(
                        guild_id, after.channel.id, text_channel_id
                    )
                )
            return
        # 他の Bot の入退室アナウンスはしない。ただし下の bot-only
        # cleanup 判定には通し、Bot だけの VC に居座らないようにする。

    vc = _as_voice_client(member.guild.voice_client)
    if vc is None or not vc.is_connected():
        return

    guild_id = member.guild.id
    bot_channel = vc.channel

    # Bot以外のメンバーがいなくなったら自動切断
    members = [m for m in bot_channel.members if not m.bot]
    if not members:
        # ユーザー意図の切断: 起動時 restore で勝手に戻らないよう session も削除
        try:
            await forget_voice_session(guild_id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(vc)
        _cleanup_guild_state(guild_id)
        logger.info(f"全員退出のため自動切断 (Guild: {guild_id})")
        return

    if member.bot:
        return

    # BotがいるVCへの入退室を通知
    joined = before.channel != bot_channel and after.channel == bot_channel
    left = before.channel == bot_channel and after.channel != bot_channel

    if joined or left:
        name = member.display_name
        if joined:
            text = f"{name}さんがにゅうしつしました"
        else:
            text = f"{name}さんがたいしつしました"
        try:
            # 合成中にBotが切断されることがあるため、再度VC状態を確認する
            vc = _as_voice_client(member.guild.voice_client)
            if vc is None or not vc.is_connected():
                return

            # ※ パフォーマンス調査のため _synth_order_lock を一旦無効化
            # async with _synth_order_lock(guild_id):
            settings = get_user_settings(member.guild.id, member.id)
            audio_data = await synthesize(text, settings, cache=True)

            vc = _as_voice_client(member.guild.voice_client)
            if vc is None or not vc.is_connected():
                return

            _ensure_queue(guild_id).append(audio_data)

            if _can_start_playback(vc):
                await play_next(guild_id, vc)
        except discord.ClientException:
            # 退出直後の race で "Not connected to voice." が起こり得る
            logger.info("入退室通知をスキップ: BotがVC未接続")
        except Exception as e:
            logger.error(f"入退室通知の音声合成エラー: {e}")


@tree.command(name="leave", description="ボイスチャンネルから切断")
async def leave(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if _has_active_voice_connection(guild):
        # 実接続中 → 切断: 起動時 restore で勝手に戻らないよう session も削除
        try:
            await forget_voice_session(guild.id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(_as_voice_client(guild.voice_client))
        _cleanup_guild_state(guild.id)
        await interaction.response.send_message("切断しました")
    else:
        # 既に切断済みなら残骸を掃除してその旨を返す
        await _reset_voice_state(guild)
        await interaction.response.send_message("ボイスチャンネルに接続していません")


@tree.command(name="vc", description="VCに接続/切断をトグル")
async def vc_toggle(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if _has_active_voice_connection(guild):
        # 実接続中 → 切断
        try:
            await forget_voice_session(guild.id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(_as_voice_client(guild.voice_client))
        _cleanup_guild_state(guild.id)
        await interaction.response.send_message("切断しました")
    else:
        # 何らかの原因で既に切断されている場合の残骸を掃除してから接続
        await _reset_voice_state(guild)
        # discord.py の Command.callback は型上 self を要求するように見えるが
        # 実体は通常の coroutine 関数として束縛されている。
        await join.callback(interaction)  # pyright: ignore[reportCallIssue]


@tree.command(name="skip", description="現在読み上げ中の音声をスキップ")
async def skip(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    vc = _as_voice_client(guild.voice_client)
    if vc is None or not _is_vc_playing(vc):
        await interaction.response.send_message("再生中の音声はありません")
        return
    try:
        vc.stop()
    except discord.ClientException:
        await interaction.response.send_message("再生中の音声はありません")
        return
    await interaction.response.send_message("スキップしました")


@tree.command(name="mute", description="指定ユーザーの読み上げをミュート")
@app_commands.describe(user="ミュートするユーザー")
async def mute_cmd(interaction: discord.Interaction, user: discord.Member):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if user.bot:
        await interaction.response.send_message("Botはミュートできません")
        return
    await add_mute(guild.id, user.id)
    await interaction.response.send_message(f"{user.display_name} をミュートしました")


@tree.command(name="unmute", description="指定ユーザーのミュートを解除")
@app_commands.describe(user="ミュート解除するユーザー")
async def unmute_cmd(interaction: discord.Interaction, user: discord.Member):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if not is_muted(guild.id, user.id):
        await interaction.response.send_message(
            f"{user.display_name} はミュートされていません"
        )
        return
    await remove_mute(guild.id, user.id)
    await interaction.response.send_message(
        f"{user.display_name} のミュートを解除しました"
    )


@tree.command(name="showmute", description="ミュート中のユーザー一覧")
async def showmute_cmd(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    mutes = guild_mutes.get(guild.id, set())
    if not mutes:
        await interaction.response.send_message("ミュート中のユーザーはいません")
        return
    lines = []
    for uid in mutes:
        member = guild.get_member(uid)
        name = member.display_name if member else f"ID: {uid}"
        lines.append(f"  {name}")
    await interaction.response.send_message(
        f"ミュート中（{len(mutes)}人）\n" + "\n".join(lines)
    )


@tree.command(name="speaker", description="自分の読み上げキャラクターを変更")
@app_commands.describe(
    character="キャラクター名（例: [VOICEVOX] ずんだもん）",
    style="スタイル名（省略時: 先頭のスタイル）",
)
async def speaker(
    interaction: discord.Interaction,
    character: str,
    style: str | None = None,
):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if not characters and not speaker_engine:
        await _refresh_speakers_if_needed()
    elif characters:
        await _refresh_missing_speakers_if_needed()

    if not characters:
        await interaction.response.send_message(
            "スピーカー情報がまだ読み込まれていません"
        )
        return

    # キャラクター名で検索（完全一致優先、無ければ最初の部分一致）
    matched_char = _match_speaker_character(character)

    if not matched_char:
        await interaction.response.send_message(
            f"キャラクター「{character}」が見つかりません"
        )
        return

    # スタイル名で検索（完全一致優先、無ければ最初の部分一致）
    styles = characters[matched_char]
    matched_style = _match_speaker_style(styles, style)

    if not matched_style:
        style_names = ", ".join(s[1] for s in styles)
        await interaction.response.send_message(
            f"「{matched_char}」に"
            f"スタイル「{style}」がありません\n"
            f"利用可能: {style_names}"
        )
        return

    speaker_id = matched_style[0]
    settings = get_user_settings(guild.id, interaction.user.id)
    settings = VoiceSettings(
        speaker_id=speaker_id,
        speed=settings.speed,
        pitch=settings.pitch,
        intonation=settings.intonation,
        volume=settings.volume,
    )
    user_settings[(guild.id, interaction.user.id)] = settings
    await save_user_setting(guild.id, interaction.user.id, settings)
    name = speakers_cache.get(speaker_id, f"ID: {speaker_id}")
    await interaction.response.send_message(f"キャラクターを「{name}」に変更しました")


def _match_speaker_character(character: str) -> str | None:
    query = character.strip().lower()
    if not query:
        return None

    partial = None
    for char_name in characters:
        lowered_key = char_name.lower()
        if query == lowered_key:
            return char_name
        if partial is None and query in lowered_key:
            partial = char_name
    return partial


def _match_speaker_style(
    styles: list[tuple[int, str]], style: str | None
) -> tuple[int, str] | None:
    if not styles:
        return None
    if style is None or not style.strip():
        return styles[0]

    partial = None
    style_query = style.lower()
    for global_id, style_name in styles:
        if style_query == style_name.lower():
            return (global_id, style_name)
        if partial is None and style_query in style_name.lower():
            partial = (global_id, style_name)
    return partial


def _interaction_option_value(
    interaction: discord.Interaction, option_name: str
) -> str | None:
    data = interaction.data if isinstance(interaction.data, dict) else {}
    for opt in data.get("options", []):
        if opt.get("name") == option_name:
            value = opt.get("value", "")
            return value if isinstance(value, str) else str(value)
    return None


@speaker.autocomplete("character")
async def speaker_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    _ = interaction
    if not characters and not speaker_engine:
        _spawn_background(_refresh_speakers_if_needed())
    elif characters:
        _schedule_missing_speaker_refresh()

    if not characters:
        return []

    query = current.lower()
    choices = []
    for char_name in characters:
        if current == "" or query in char_name.lower():
            choices.append(app_commands.Choice(name=char_name, value=char_name))
            if len(choices) >= 25:
                break
    return choices


@speaker.autocomplete("style")
async def speaker_style_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if characters:
        _schedule_missing_speaker_refresh()

    char_input = _interaction_option_value(interaction, "character")

    if not char_input or not characters:
        return []

    # キャラクター名でマッチ
    matched_char = _match_speaker_character(char_input)
    if not matched_char:
        return []

    styles = characters[matched_char]
    choices = []
    for _, style_name in styles:
        if current == "" or current.lower() in style_name.lower():
            choices.append(app_commands.Choice(name=style_name, value=style_name))
            if len(choices) >= 25:
                break
    return choices


@tree.command(name="voice", description="自分の読み上げ音声パラメータを変更")
@app_commands.describe(
    speed="話速（0.5〜2.0、デフォルト: 1.0）",
    pitch="音高（-0.15〜0.15、デフォルト: 0.0）",
    intonation="抑揚（0.0〜2.0、デフォルト: 1.0）",
    volume="音量（0.0〜2.0、デフォルト: 1.0）",
)
async def voice(
    interaction: discord.Interaction,
    speed: float | None = None,
    pitch: float | None = None,
    intonation: float | None = None,
    volume: float | None = None,
):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    settings = get_user_settings(guild.id, interaction.user.id)

    # 指定されたパラメータのみ更新
    new_speed = settings.speed if speed is None else max(0.5, min(2.0, speed))
    new_pitch = settings.pitch if pitch is None else max(-0.15, min(0.15, pitch))
    new_intonation = (
        settings.intonation if intonation is None else max(0.0, min(2.0, intonation))
    )
    new_volume = settings.volume if volume is None else max(0.0, min(2.0, volume))

    # 何も指定されなかったら現在の設定を表示
    if speed is None and pitch is None and intonation is None and volume is None:
        speaker_name = speakers_cache.get(
            settings.speaker_id, f"ID: {settings.speaker_id}"
        )
        await interaction.response.send_message(
            f"現在の音声設定:\n"
            f"  キャラクター: {speaker_name}\n"
            f"  話速: {settings.speed}\n"
            f"  音高: {settings.pitch}\n"
            f"  抑揚: {settings.intonation}\n"
            f"  音量: {settings.volume}"
        )
        return

    new_settings = VoiceSettings(
        speaker_id=settings.speaker_id,
        speed=new_speed,
        pitch=new_pitch,
        intonation=new_intonation,
        volume=new_volume,
    )
    user_settings[(guild.id, interaction.user.id)] = new_settings
    await save_user_setting(guild.id, interaction.user.id, new_settings)

    changed = []
    if speed is not None:
        changed.append(f"話速: {new_speed}")
    if pitch is not None:
        changed.append(f"音高: {new_pitch}")
    if intonation is not None:
        changed.append(f"抑揚: {new_intonation}")
    if volume is not None:
        changed.append(f"音量: {new_volume}")

    await interaction.response.send_message(
        "音声設定を変更しました\n  " + "\n  ".join(changed)
    )


@tree.command(name="dict", description="読み上げ辞書の設定")
async def dict_cmd(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    content, view = build_dict_message(guild.id)
    await interaction.response.send_message(content=content, view=view)


@tree.command(name="help", description="コマンド一覧を表示")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_help_embed())


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    vc = _as_voice_client(message.guild.voice_client)
    if vc is None or not vc.is_connected():
        return

    # /join を実行したチャンネルのみ読み上げ
    if read_channels.get(message.guild.id) != message.channel.id:
        return

    # ミュートされたユーザーは読み上げない
    if is_muted(message.guild.id, message.author.id):
        return

    # 「;」で始まるメッセージは読み上げ対象外（チャットのみ用途）
    if message.content.startswith(";"):
        return

    text = clean_text(message.clean_content)
    attachment_notice = _build_attachment_notice(message.attachments)
    if not text and not attachment_notice:
        return

    # 辞書で置換（ユーザ辞書が先、built-in は後でユーザ側が優先）
    text = apply_dict(message.guild.id, text)
    # 通常運用の応答速度を優先するため、重い built-in 読み補正は一時無効化。
    # text = apply_reading_corrections(text)

    # 長すぎるメッセージは切り詰め
    if len(text) > MAX_READ_LENGTH:
        text = text[:MAX_READ_LENGTH] + "、いかりゃく"

    # 添付ファイルがあれば末尾に通知（添付のみの場合は通知だけ読み上げる）
    if attachment_notice:
        text = f"{text}、{attachment_notice}" if text else attachment_notice

    guild_id = message.guild.id

    # キューが満杯なら合成してもドロップされるだけ。TTS コスト無駄なのでスキップ。
    existing_queue = queues.get(guild_id)
    if existing_queue is not None and len(existing_queue) >= QUEUE_MAXLEN:
        return

    # ユーザ単位レートリミットで abuse コストを頭打ちにする
    if not _rate_limit_try_consume(guild_id, message.author.id):
        return

    # 合成→queue追加をロックで包んで到着順に並べる（並行タスクによる逆転防止）
    # ※ パフォーマンス調査のため一旦無効化（直列化が他Bot比で遅延の主因の疑い）
    try:
        # async with _synth_order_lock(guild_id):
        settings = get_user_settings(guild_id, message.author.id)
        audio_data = await synthesize(text, settings)
        _ensure_queue(guild_id).append(audio_data)
    except aiohttp.ClientError:
        logger.warning("音声合成エンジンに接続できません（再起動中の可能性）")
        now = time.monotonic()
        last = engine_error_notified_at.get(guild_id, 0.0)
        if now - last >= ENGINE_ERROR_NOTIFY_INTERVAL:
            engine_error_notified_at[guild_id] = now
            await message.channel.send(
                "音声エンジンに接続できません。しばらくお待ちください。"
            )
        return
    except Exception as e:
        logger.error(f"音声合成エラー: {e}")
        return

    if _can_start_playback(vc):
        await play_next(guild_id, vc)


# Discord ログイン時の 503 等のリトライ上限（指数バックオフ: 5, 10, 20, 40, 80 秒）
MAX_LOGIN_RETRIES = 5
# トークン無効化（再生成・失効・4004認証失敗）時のexit前 sleep。
# コンテナの restart loop が即連続で回るのを防ぎ、ログ汚染とクォータ消費を抑える。
TOKEN_INVALID_BACKOFF_SECONDS = 300


def _log_and_backoff_for_token_invalid(error_msg: str) -> None:
    """トークン無効と判定したら警告ログを出してから長めに sleep する。

    呼び出し側で raise を続けることで、コンテナ即再起動による fast
    restart loop を緩和する（exit 自体は呼び出し側の bare raise で行う）。
    """
    logger.error(
        f"{error_msg} — DISCORD_TOKEN または DISCORD_TOKENS を確認してください "
        f"（Discord Developer Portal で再生成 → 環境変数を更新 → redeploy）"
        f" / {TOKEN_INVALID_BACKOFF_SECONDS}秒待機して exit します"
    )
    time.sleep(TOKEN_INVALID_BACKOFF_SECONDS)


def _run_single_bot(discord_token: str):
    """単一トークンでBotを起動（Discord API障害時は指数バックオフで再試行）。

    トークン無効（401 LoginFailure / 4004）は永続的失敗としてリトライせず、
    長めに sleep してから raise する（コンテナ fast restart loop 回避）。
    """
    for attempt in range(MAX_LOGIN_RETRIES):
        try:
            client.run(discord_token)
            break
        except discord.LoginFailure as e:
            _log_and_backoff_for_token_invalid(f"Discordログイン失敗: {e}")
            raise
        except discord.ConnectionClosed as e:
            if getattr(e, "code", None) == 4004:
                _log_and_backoff_for_token_invalid(
                    f"Discord認証失敗 (4004): セッション中にトークン無効化 ({e})"
                )
            raise
        except discord.DiscordServerError as e:
            if attempt == MAX_LOGIN_RETRIES - 1:
                logger.error(f"最大リトライ回数到達、諦めます: {e}")
                raise
            wait = 5 * (2**attempt)
            logger.warning(
                f"Discord API一時障害 ({attempt + 1}/{MAX_LOGIN_RETRIES}): {e}"
                f" → {wait}秒待機して再試行"
            )
            time.sleep(wait)


def _terminate_processes(processes: list[subprocess.Popen]) -> None:
    """全Bot子プロセスを停止する（SIGTERM → 10秒待機 → SIGKILL）。"""
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@dataclass
class _ChildBotSlot:
    instance: int
    token: str
    process: subprocess.Popen
    # 直近の終了時刻（クラッシュループ判定用）
    failure_times: deque[float] = field(default_factory=deque)
    # 次回の再起動時刻（time.monotonic 基準）。0.0 = 再起動待ち無し
    next_restart_at: float = 0.0


def _spawn_child_bot(token: str, instance: int, script_path: str) -> subprocess.Popen:
    """子Botプロセスを起動する。"""
    child_env = os.environ.copy()
    child_env["DISCORD_TOKEN"] = token
    child_env["DISCORD_TOKENS"] = ""
    child_env["MULTIBOT_CHILD"] = "1"
    child_env["BOT_INSTANCE_INDEX"] = str(instance)
    # 親プロセスで事前に実行済みのため、子側はマイグレーションを行わない
    child_env["RUN_DB_MIGRATIONS"] = "0"
    proc = subprocess.Popen([sys.executable, script_path], env=child_env)
    logger.info(f"Botプロセス起動: instance={instance}, pid={proc.pid}")
    return proc


def _run_multi_bots(discord_tokens: list[str]) -> None:
    """複数トークンを子プロセスとして並列起動し、落ちたら自動再起動する。

    各 slot の backoff sleep を統合し、最も近い再起動時刻まで一括で待機することで
    複数Bot同時クラッシュ時も復旧時間が線形に伸びないようにしている。
    クラッシュループ（BOT_CRASH_WINDOW_SECONDS 内に BOT_CRASH_THRESHOLD 回終了）
    を検知したら親も停止して、コンテナレベルのオートヒールに委ねる（fail-fast）。
    """
    script_path = os.path.abspath(__file__)
    slots: list[_ChildBotSlot] = []
    shutdown_requested = False
    logger.info(f"複数Botモードで起動: {len(discord_tokens)}プロセス")

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        # docker stop などの SIGTERM を KeyboardInterrupt 経路に集約
        nonlocal shutdown_requested
        shutdown_requested = True
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, _shutdown)
    try:
        for idx, token in enumerate(discord_tokens, start=1):
            slots.append(
                _ChildBotSlot(
                    instance=idx,
                    token=token,
                    process=_spawn_child_bot(token, idx, script_path),
                )
            )

        while not shutdown_requested:
            now = time.monotonic()

            # 1. 期限到来済みの slot を一括 spawn（並列復旧）
            for slot in slots:
                if shutdown_requested:
                    break
                if slot.next_restart_at and now >= slot.next_restart_at:
                    slot.process = _spawn_child_bot(
                        slot.token, slot.instance, script_path
                    )
                    slot.next_restart_at = 0.0

            # 2. 動作中 slot を poll → 終了検知 → backoff 計算
            for slot in slots:
                if slot.next_restart_at:
                    continue  # 再起動待ち中
                code = slot.process.poll()
                if code is None:
                    continue
                # クラッシュ履歴を更新（時間窓外は捨てる）
                slot.failure_times.append(now)
                while (
                    slot.failure_times
                    and now - slot.failure_times[0] > BOT_CRASH_WINDOW_SECONDS
                ):
                    slot.failure_times.popleft()
                if len(slot.failure_times) >= BOT_CRASH_THRESHOLD:
                    raise RuntimeError(
                        f"クラッシュループ検出 instance={slot.instance}: "
                        f"{BOT_CRASH_WINDOW_SECONDS}秒に"
                        f"{len(slot.failure_times)}回終了 (last_exit={code})"
                    )
                # 指数バックオフ（1, 2, 4, 8, 16秒、上限 60秒）
                backoff = min(
                    2 ** (len(slot.failure_times) - 1),
                    BOT_RESTART_BACKOFF_MAX_SECONDS,
                )
                slot.next_restart_at = now + backoff
                logger.warning(
                    f"Botプロセス終了 instance={slot.instance} "
                    f"pid={slot.process.pid} exit={code} "
                    f"→ {backoff}秒後に再起動 "
                    f"(直近終了 {len(slot.failure_times)}/{BOT_CRASH_THRESHOLD})"
                )

            # 3. 次のイベント時刻まで待機（最も近い再起動 or poll間隔の小さい方）
            pending = [s.next_restart_at for s in slots if s.next_restart_at]
            if pending:
                sleep_for = min(
                    BOT_POLL_INTERVAL_SECONDS,
                    max(0.0, min(pending) - time.monotonic()),
                )
            else:
                sleep_for = BOT_POLL_INTERVAL_SECONDS
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        logger.info("終了シグナルを受信、全Botプロセスを停止します")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        _terminate_processes([slot.process for slot in slots])


if __name__ == "__main__":
    tokens = _resolve_discord_tokens()
    if not tokens:
        raise RuntimeError(
            "DISCORD_TOKEN または DISCORD_TOKENS environment variable が必要です"
        )
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is required")
    if not ENGINES:
        raise RuntimeError("VOICEVOX_URL など、少なくとも1つのTTSエンジンURLが必要です")

    # 利用可能なら uvloop に差し替え（イベントループが 2〜4 倍高速）。
    # Windows や未インストール環境では標準 asyncio のままフォールバック。
    try:
        import uvloop

        uvloop.install()
        logger.info("uvloop を有効化しました")
    except ImportError:
        logger.info("uvloop 未インストール、標準 asyncio ループで起動")

    logger.info(
        f"起動モード: {'child' if IS_MULTIBOT_CHILD else 'single'}, "
        f"instance={BOT_INSTANCE_INDEX}, tokens={len(tokens)}"
    )
    if len(tokens) > 1 and not IS_MULTIBOT_CHILD:
        logger.info("複数Botモードの事前処理としてDBマイグレーションを実行します")
        asyncio.run(
            migration_runner.run_pending_migrations(DATABASE_URL, logger=logger)
        )
        _run_multi_bots(tokens)
    else:
        _run_single_bot(tokens[0])
