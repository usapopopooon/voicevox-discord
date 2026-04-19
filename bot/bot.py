import asyncio
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
from dataclasses import dataclass, field

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
if _LOG_LEVEL_NAME not in logging._nameToLevel:
    logger.warning(f"LOG_LEVEL='{_LOG_LEVEL_NAME}' は未知のため INFO にフォールバック")

# 設定（環境変数で切り替え）
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_TOKENS_RAW = os.getenv("DISCORD_TOKENS", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_SPEAKER = int(os.getenv("DEFAULT_SPEAKER_ID", "3"))


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

# 子プロセスの自動再起動（指数バックオフ + クラッシュループ検出）
BOT_RESTART_BACKOFF_MAX_SECONDS = 60
BOT_CRASH_WINDOW_SECONDS = 300
BOT_CRASH_THRESHOLD = 5
BOT_POLL_INTERVAL_SECONDS = 2

# 各エンジンの定義（名前, 環境変数, デフォルトURL, IDオフセット）
# IDオフセットでエンジン間のスピーカーID衝突を回避
_ENGINE_DEFS = [
    ("VOICEVOX", "VOICEVOX_URL", "http://localhost:50021", 0),
    ("COEIROINK", "COEIROINK_URL", "", 10000),
    ("SHAREVOX", "SHAREVOX_URL", "", 20000),
]
ENGINES: list[tuple[str, str, int]] = [  # (name, url, offset)
    (name, url, offset)
    for name, env, default, offset in _ENGINE_DEFS
    if (url := os.getenv(env, default))
]

logger.info(f"TTS_ENGINES: {[(n, u) for n, u, _ in ENGINES]}")
logger.info(f"DEFAULT_SPEAKER_ID: {DEFAULT_SPEAKER}")

# Intents設定（message_contentはテキスト読み上げに必須）
intents = discord.Intents.default()
intents.message_content = True

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
client = TtsClient(intents=intents, chunk_guilds_at_startup=False)
tree = app_commands.CommandTree(client)

# ギルドあたりの再生キュー最大長。1件あたり最大 ~500KB なので maxlen=64 で ~32MB 上限。
# スパム時は古い音声から自動ドロップしてメモリ使用量を制限する。
QUEUE_MAXLEN = 64

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

# VC 復旧の多重起動防止 + リトライ設定
_vc_reconnect_inflight: set[int] = set()
VC_RECONNECT_MAX_ATTEMPTS = 5
VC_RECONNECT_BACKOFF_BASE_SECONDS = 2
VC_RECONNECT_BACKOFF_MAX_SECONDS = 60

# /leave 等のユーザー意図切断後の guard 期間。DB の forget が失敗していても
# この期間内は復旧をブロックして「leave したのに勝手に戻る」を防ぐ。
_recently_left_at: dict[int, float] = {}
RECENTLY_LEFT_GUARD_SECONDS = 30.0

# fire-and-forget タスクの参照保持（CPython の GC で消されないように）
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """create_task しつつ参照を保持し、完了時に自動回収する。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _mark_recently_left(guild_id: int) -> None:
    """ユーザー意図の切断を記録。直後の自動復旧を抑止するために使う。"""
    _recently_left_at[guild_id] = time.monotonic()


def _is_recently_left(guild_id: int) -> bool:
    """直近 RECENTLY_LEFT_GUARD_SECONDS 以内に意図的切断されたか。
    期限切れエントリは lazy 削除する。"""
    ts = _recently_left_at.get(guild_id)
    if ts is None:
        return False
    if time.monotonic() - ts < RECENTLY_LEFT_GUARD_SECONDS:
        return True
    _recently_left_at.pop(guild_id, None)
    return False


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
    """VC が接続中かを安全に判定する（遷移中の ClientException を吸収）"""
    try:
        return vc.is_connected()
    except discord.ClientException:
        return False


def _is_vc_playing(vc: discord.VoiceClient) -> bool:
    """VC が再生中かを安全に判定する（遷移中の ClientException を吸収）"""
    try:
        return vc.is_playing()
    except discord.ClientException:
        return False


async def _safe_disconnect(vc: discord.VoiceClient) -> None:
    """VC切断。既に切断済みなどで例外が出ても無視する。"""
    try:
        await vc.disconnect()
    except Exception as e:
        logger.warning(f"切断でエラー: {e}")


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
    "(爆)": "ばくわら",
    "(苦笑)": "くわら",
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
    "XD": "だいわらい",
    "xD": "だいわらい",
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
_KAOMOJI_DICT: dict[str, str] = dict(_BUILTIN_KAOMOJI_DICT)
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
    """
    global _KAOMOJI_NORMALIZED_MAX_LEN, _KAOMOJI_PATTERN

    _KAOMOJI_NORMALIZED_DICT.clear()
    for face, reading in _KAOMOJI_DICT.items():
        normalized = _normalize_kaomoji_for_lookup(face)
        _KAOMOJI_NORMALIZED_DICT.setdefault(normalized, reading)
    _KAOMOJI_NORMALIZED_MAX_LEN = (
        max((len(k) for k in _KAOMOJI_NORMALIZED_DICT), default=0)
        if _KAOMOJI_DICT
        else 0
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
_JP_NET_SLANG_TRIGGER_CHARS = {"草", "w", "W", "ｗ", "Ｗ"}


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


# 日本語圏のネットスラング（笑い表現）
_JP_NET_SLANG_PATTERN = re.compile(
    r"(?P<kusa>(?<![\w.])草+(?![\w.]))"
    r"|(?P<w>(?<![A-Za-z0-9.])[wｗ]{2,}(?![A-Za-z0-9.]))"
)


def _replace_jp_net_slang(text: str) -> str:
    """日本語ネットスラング（草 / www / ｗｗ）を読み仮名に置換する。"""
    return _JP_NET_SLANG_PATTERN.sub("わらい", text)


# 高頻度 Unicode 絵文字の読み替え。必要最小限に絞ってコストを抑える。
_UNICODE_EMOJI_READING: dict[str, str] = {
    "☺️": "にっこり",
    "☺": "にっこり",
    "😀": "にこにこ",
    "😁": "にっこり",
    "😂": "わらい",
    "🤣": "ばくわら",
    "😆": "わらい",
    "😊": "えがお",
    "😍": "だいすき",
    "😘": "ちゅ",
    "🥰": "だいすき",
    "😉": "ういんく",
    "🤔": "うーん",
    "😢": "かなしい",
    "😭": "おおなき",
    "😡": "おこる",
    "😱": "びっくり",
    "😴": "ねむい",
    "🥺": "うるうる",
    "🙏": "おねがい",
    "🙇": "ぺこり",
    "👍": "ぐっど",
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
    """Unicode絵文字を読み仮名に置換する。

    既知の絵文字は `_UNICODE_EMOJI_READING` を優先し、
    未知の絵文字も `えもじ_<shortcode>` 形式で読み上げ可能にする。
    """
    if emoji_lib is not None:

        def _replace(chars: str, data: dict) -> str:
            reading = _UNICODE_EMOJI_READING.get(chars)
            if reading is not None:
                return reading
            normalized_chars = _EMOJI_SKIN_TONE_MODIFIER_PATTERN.sub("", chars)
            reading = _UNICODE_EMOJI_READING.get(normalized_chars)
            if reading is not None:
                return reading
            shortcode = emoji_lib.demojize(normalized_chars, delimiters=("", ""))
            shortcode = shortcode.replace(":", "")
            guessed = _shortcode_to_reading(shortcode)
            if guessed is not None:
                return guessed
            if not shortcode:
                return chars
            return "えもじ_" + shortcode

        return emoji_lib.replace_emoji(text, _replace)

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

# built-in 読み辞書のデフォルトスナップショット。
# DB初期投入やフォールバックで利用する（runtime dict の変更に影響されない）。
_DEFAULT_READING_CORRECTIONS: dict[str, str] = dict(_BUILTIN_READING_CORRECTIONS)
_DEFAULT_ENGLISH_WORD_READINGS: dict[str, str] = dict(_BUILTIN_ENGLISH_WORD_READINGS)


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
_RECENT_SYNTH_TTL_SECONDS = 20.0
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

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    guild_id BIGINT NOT NULL DEFAULT 0,
                    user_id BIGINT NOT NULL,
                    speaker_id INTEGER NOT NULL DEFAULT 3,
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
    """DBからギルドの辞書設定をメモリにロード"""
    async with _require_db_pool().acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, word, reading FROM guild_dicts")
    guild_dicts.clear()
    _dict_patterns.clear()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_dicts:
            guild_dicts[gid] = {}
        guild_dicts[gid][row["word"]] = row["reading"]
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

        db_jp = {
            row["word"]: row["reading"] for row in rows if row["dict_type"] == "jp"
        }
        db_en = {
            row["word"]: row["reading"] for row in rows if row["dict_type"] == "en"
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


async def add_dict_entry(guild_id: int, word: str, reading: str):
    """辞書エントリをメモリ/DBに保存しパターンキャッシュを無効化する"""
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


async def _fetch_voice_session(guild_id: int) -> tuple[int, int] | None:
    """guild_id の VC セッションを返す。無ければ None。"""
    async with _require_db_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT voice_channel_id, text_channel_id "
            "FROM active_voice_sessions WHERE guild_id = $1",
            guild_id,
        )
    if row is None:
        return None
    return (row["voice_channel_id"], row["text_channel_id"])


async def _reconnect_vc(
    guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """元の VC へ再接続する（指数バックオフ・回数制限・多重起動防止）。

    呼び出し元（on_voice_state_update / 起動時 restore）に関わらず、同じ
    guild_id について同時に走らないよう _vc_reconnect_inflight でガードする。
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

        for attempt in range(VC_RECONNECT_MAX_ATTEMPTS):
            existing = guild.voice_client
            if existing and _is_vc_connected(existing):
                logger.info(f"VC既に接続中、復旧不要 guild={guild_id}")
                return
            try:
                await channel.connect()
                queues[guild_id] = _new_queue()
                read_channels[guild_id] = text_channel_id
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


async def _maybe_recover_voice_session(guild_id: int) -> None:
    """on_voice_state_update から呼ばれ、DB にセッションが残っていれば復旧する。"""
    # /leave 直後の guard：DB の forget が失敗してもユーザー意図を尊重する
    if _is_recently_left(guild_id):
        logger.info(
            f"VC復旧抑止 guild={guild_id}: 直近のユーザー意図の切断（guard期間内）"
        )
        return
    try:
        session = await _fetch_voice_session(guild_id)
    except Exception as e:
        logger.warning(f"VCセッション照会失敗 guild={guild_id}: {e}")
        return
    if session is None:
        return  # ユーザー意図の切断 → 復旧しない
    voice_channel_id, text_channel_id = session
    await _reconnect_vc(guild_id, voice_channel_id, text_channel_id)


async def _restore_voice_sessions_on_startup() -> None:
    """起動時に DB から全 VC セッションを順次復旧する（並列度1で rate limit 安全）。"""
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
    text = _replace_kaomoji(text)
    if _contains_any_char(text, _KAOMOJI_OPENERS):
        text = _replace_kaomoji_variant(text)
    if _contains_any_char(text, _WESTERN_EMOTICON_TRIGGER_CHARS):
        text = _replace_western_emoticon(text)
    if _contains_any_char(text, _JP_NET_SLANG_TRIGGER_CHARS):
        text = _replace_jp_net_slang(text)
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
        stderr=subprocess.DEVNULL,
    )


# --- TTS エンジン ---


async def fetch_speakers():
    """全エンジンからスピーカー一覧を取得して統合キャッシュ"""
    speakers_cache.clear()
    speaker_engine.clear()
    characters.clear()

    session = await get_http_session()
    for engine_name, engine_url, offset in ENGINES:
        try:
            async with session.get(f"{engine_url}/speakers") as resp:
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

            logger.info(f"スピーカー取得成功: {engine_name} ({count}件)")
        except Exception as e:
            logger.warning(f"スピーカー取得失敗: {engine_name}: {e}")

    logger.info(f"スピーカー一覧合計: {len(speakers_cache)}件")


async def _refresh_speakers_if_needed() -> None:
    """speaker_engine が空のときにスピーカー一覧を再取得する（短時間の連打を抑制）。"""
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
    # 注意: DEFAULT_SPEAKER は VOICEVOX のデフォルト値（3=ずんだもん）が前提。
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
    """指定候補で音声合成を1回実行する。"""
    session = await get_http_session()
    params = {"text": text, "speaker": real_id}
    async with session.post(f"{engine_url}/audio_query", params=params) as resp:
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

    _candidate_fail_until.pop(pair, None)
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

        def after_play(error):
            if error:
                logger.error(f"再生エラー: {error}")
            future = asyncio.run_coroutine_threadsafe(
                play_next(guild_id, vc), client.loop
            )

            def _log_future_exception(fut):
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

        await add_dict_entry(self.guild_id, word, reading)

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
    await tree.sync()
    logger.info(f"Botログイン: {client.user} (ID: {client.user.id})")
    logger.info("スラッシュコマンドを同期しました")

    try:
        await fetch_speakers()
    except Exception as e:
        logger.warning(f"スピーカー一覧の取得に失敗しました: {e}")

    # 起動時の VC 復旧（プロセス再起動・デプロイ後の復帰用）
    # fetch_speakers より後にすることで TTS が使えない状態での接続を避ける。
    # background 化して on_ready 自体は即座に return（ゲートウェイ再接続時の
    # on_ready 再発火と長時間 await の重複を避ける）
    _spawn_background(_restore_voice_sessions_on_startup())


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


@tree.command(name="join", description="ボイスチャンネルに接続")
async def join(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if not interaction.user.voice:
        await interaction.response.send_message("先にボイスチャンネルに入ってください")
        return

    channel = interaction.user.voice.channel

    me = guild.me
    if me is None and client.user is not None:
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

    was_connected = guild.voice_client is not None
    try:
        if was_connected:
            await guild.voice_client.move_to(channel)
        else:
            await channel.connect()
    except Exception as e:
        await interaction.response.send_message(f"VCへの接続に失敗しました: {e}")
        return

    if was_connected:
        # move_to の場合: 既存キュー（未再生の音声）は保持
        _ensure_queue(guild.id)
    else:
        # 新規接続: 切断クリーンアップ漏れ等で残存する古いキューを破棄
        queues[guild.id] = _new_queue()
    read_channels[guild.id] = interaction.channel_id

    # 再起動・切断時に元の VC へ復旧できるようセッションを永続化
    try:
        await record_voice_session(guild.id, channel.id, interaction.channel_id)
    except Exception as e:
        logger.warning(f"VCセッション保存に失敗: {e}")

    embed = discord.Embed(
        title="読み上げBot — コマンド一覧",
        description=(
            f"「{channel.name}」に接続しました\nこのチャンネルのメッセージを読み上げます\n\n"
            "`/vc` — VCに接続/切断（トグル）\n"
            "`/join` — VCに接続\n"
            "`/leave` — VCから切断\n"
            "`/skip` — 読み上げをスキップ\n"
            "`/speaker` — キャラクター変更\n"
            "`/voice` — 話速・音高・抑揚・音量\n"
            "`/dict` — 読み上げ辞書の管理\n"
            "`/mute` — ユーザーをミュート\n"
            "`/unmute` — ミュート解除\n"
            "`/showmute` — ミュート一覧"
        ),
        color=0x00B0F4,
    )
    await interaction.response.send_message(embed=embed)

    # 接続時に音声で挨拶
    try:
        async with _synth_order_lock(guild.id):
            settings = get_user_settings(guild.id, interaction.user.id)
            audio_data = await synthesize("せつぞくしました", settings, cache=True)
            vc = guild.voice_client
            if vc and _is_vc_connected(vc):
                queues[guild.id].append(audio_data)
        vc = guild.voice_client
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
        # 他の Bot のイベントは無視
        if client.user is None or member.id != client.user.id:
            return

        guild_id = member.guild.id
        if after.channel is None:
            # Bot 自身の切断 → ギルド状態をクリーンアップ
            _cleanup_guild_state(guild_id)
            # DB にセッションが残っていれば（=ユーザー意図ではない切断）、復旧を試みる
            _spawn_background(_maybe_recover_voice_session(guild_id))
        else:
            # Bot 自身の再接続 → 残キューがあれば再生再開
            vc = member.guild.voice_client
            queue = queues.get(guild_id)
            if vc and queue and _can_start_playback(vc):
                await play_next(guild_id, vc)
        return

    vc = member.guild.voice_client
    if not vc or not vc.is_connected():
        return

    guild_id = member.guild.id
    bot_channel = vc.channel

    # Bot以外のメンバーがいなくなったら自動切断
    members = [m for m in bot_channel.members if not m.bot]
    if not members:
        # ユーザー意図の切断扱い: DB forget が失敗しても guard で復旧抑止
        _mark_recently_left(guild_id)
        try:
            await forget_voice_session(guild_id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(vc)
        _cleanup_guild_state(guild_id)
        logger.info(f"全員退出のため自動切断 (Guild: {guild_id})")
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
            vc = member.guild.voice_client
            if not vc or not vc.is_connected():
                return

            async with _synth_order_lock(guild_id):
                settings = get_user_settings(member.guild.id, member.id)
                audio_data = await synthesize(text, settings, cache=True)

                vc = member.guild.voice_client
                if not vc or not vc.is_connected():
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

    if guild.voice_client:
        # ユーザー意図の切断: DB forget が失敗しても guard で復旧抑止
        _mark_recently_left(guild.id)
        try:
            await forget_voice_session(guild.id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(guild.voice_client)
        _cleanup_guild_state(guild.id)
        await interaction.response.send_message("切断しました")
    else:
        await interaction.response.send_message("ボイスチャンネルに接続していません")


@tree.command(name="vc", description="VCに接続/切断をトグル")
async def vc_toggle(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if guild.voice_client:
        # ユーザー意図の切断: DB forget が失敗しても guard で復旧抑止
        _mark_recently_left(guild.id)
        try:
            await forget_voice_session(guild.id)
        except Exception as e:
            logger.warning(f"VCセッション削除に失敗: {e}")
        await _safe_disconnect(guild.voice_client)
        _cleanup_guild_state(guild.id)
        await interaction.response.send_message("切断しました")
    else:
        await join.callback(interaction)


@tree.command(name="skip", description="現在読み上げ中の音声をスキップ")
async def skip(interaction: discord.Interaction):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    vc = guild.voice_client
    if not vc or not _is_vc_playing(vc):
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
    character="キャラクター名（例: ずんだもん）",
    style="スタイル名（省略時: ノーマル）",
)
async def speaker(
    interaction: discord.Interaction,
    character: str,
    style: str = "ノーマル",
):
    guild = await _require_guild_interaction(interaction)
    if guild is None:
        return

    if not characters:
        await interaction.response.send_message(
            "スピーカー情報がまだ読み込まれていません"
        )
        return

    # キャラクター名で検索（完全一致優先、無ければ最初の部分一致）
    matched_char = None
    query = character.lower()
    for char_name in characters:
        if query == char_name.lower():
            matched_char = char_name
            break
        if matched_char is None and query in char_name.lower():
            matched_char = char_name

    if not matched_char:
        await interaction.response.send_message(
            f"「{character}」に一致するキャラクターが見つかりません"
        )
        return

    # スタイル名で検索（完全一致優先、無ければ最初の部分一致）
    styles = characters[matched_char]
    matched_style = None
    style_query = style.lower()
    for global_id, style_name in styles:
        if style_query == style_name.lower():
            matched_style = (global_id, style_name)
            break
        if matched_style is None and style_query in style_name.lower():
            matched_style = (global_id, style_name)

    if not matched_style:
        style_names = ", ".join(s[1] for s in styles)
        await interaction.response.send_message(
            f"「{matched_char}」にスタイル「{style}」がありません\n"
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


@speaker.autocomplete("character")
async def speaker_char_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not characters:
        return []
    choices = []
    for char_name in characters:
        if current == "" or current.lower() in char_name.lower():
            choices.append(app_commands.Choice(name=char_name, value=char_name))
            if len(choices) >= 25:
                break
    return choices


@speaker.autocomplete("style")
async def speaker_style_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    # 入力中のcharacterオプションを取得
    char_input = None
    data = interaction.data if isinstance(interaction.data, dict) else {}
    for opt in data.get("options", []):
        if opt["name"] == "character":
            char_input = opt.get("value", "")
            break

    if not char_input or not characters:
        return []

    # キャラクター名でマッチ
    matched_char = None
    for char_name in characters:
        if char_input.lower() in char_name.lower():
            matched_char = char_name
            if char_input.lower() == char_name.lower():
                break

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


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not message.guild:
        return

    vc = message.guild.voice_client
    if not vc or not vc.is_connected():
        return

    # /join を実行したチャンネルのみ読み上げ
    if read_channels.get(message.guild.id) != message.channel.id:
        return

    # ミュートされたユーザーは読み上げない
    if is_muted(message.guild.id, message.author.id):
        return

    text = clean_text(message.clean_content)
    if not text:
        return

    # 辞書で置換（ユーザ辞書が先、built-in は後でユーザ側が優先）
    text = apply_dict(message.guild.id, text)
    text = apply_reading_corrections(text)

    # 長すぎるメッセージは切り詰め
    if len(text) > MAX_READ_LENGTH:
        text = text[:MAX_READ_LENGTH] + "、いかりゃく"

    guild_id = message.guild.id

    # キューが満杯なら合成してもドロップされるだけ。TTS コスト無駄なのでスキップ。
    existing_queue = queues.get(guild_id)
    if existing_queue is not None and len(existing_queue) >= QUEUE_MAXLEN:
        return

    # ユーザ単位レートリミットで abuse コストを頭打ちにする
    if not _rate_limit_try_consume(guild_id, message.author.id):
        return

    # 合成→queue追加をロックで包んで到着順に並べる（並行タスクによる逆転防止）
    try:
        async with _synth_order_lock(guild_id):
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

    def _shutdown(_signum, _frame):
        # docker stop / Railway shutdown の SIGTERM を KeyboardInterrupt 経路に集約
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
