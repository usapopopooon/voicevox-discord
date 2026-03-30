import asyncio
import io
import logging
import os
import re
from collections import deque
from dataclasses import dataclass

import aiohttp
import asyncpg
import discord
from discord import app_commands, ui
from dotenv import load_dotenv

load_dotenv()

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 設定（環境変数で切り替え）
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DEFAULT_SPEAKER = int(os.getenv("DEFAULT_SPEAKER_ID", "3"))

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

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ギルドごとの再生キューと読み上げ対象チャンネル
queues: dict[int, deque] = {}
read_channels: dict[int, int] = {}  # guild_id -> channel_id


@dataclass
class VoiceSettings:
    speaker_id: int = DEFAULT_SPEAKER
    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    volume: float = 1.0


# メモリキャッシュ
user_settings: dict[int, VoiceSettings] = {}
speakers_cache: dict[int, str] = {}  # global_id -> 表示名
# global_id -> (engine_url, real_speaker_id)
speaker_engine: dict[int, tuple[str, int]] = {}
# キャラクター名 -> [(global_id, スタイル名)]
characters: dict[str, list[tuple[int, str]]] = {}
guild_dicts: dict[int, dict[str, str]] = {}
guild_mutes: dict[int, set[int]] = {}  # guild_id -> set of muted user_ids

# テキスト前処理用の正規表現
URL_PATTERN = re.compile(r"https?://\S+")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w+):\d+>")
MAX_READ_LENGTH = 100

# DB接続プール
db_pool: asyncpg.Pool | None = None


# --- DB ---


async def init_db():
    """DB接続プールを作成し、テーブルを初期化する（リトライあり）"""
    global db_pool
    for attempt in range(5):
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL)
            break
        except (OSError, asyncpg.PostgresError) as e:
            if attempt < 4:
                logger.warning(f"DB接続失敗 ({attempt + 1}/5): {e}、2秒後にリトライ")
                await asyncio.sleep(2)
            else:
                raise
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                speaker_id INTEGER NOT NULL DEFAULT 3,
                speed REAL NOT NULL DEFAULT 1.0,
                pitch REAL NOT NULL DEFAULT 0.0,
                intonation REAL NOT NULL DEFAULT 1.0,
                volume REAL NOT NULL DEFAULT 1.0
            )
        """)
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
    logger.info("DB初期化完了")


async def load_user_settings():
    """DBからユーザー設定をメモリにロード"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, speaker_id, speed, pitch, intonation, volume "
            "FROM user_settings"
        )
    user_settings.clear()
    for row in rows:
        user_settings[row["user_id"]] = VoiceSettings(
            speaker_id=row["speaker_id"],
            speed=row["speed"],
            pitch=row["pitch"],
            intonation=row["intonation"],
            volume=row["volume"],
        )
    logger.info(f"ユーザー設定を読み込みました: {len(user_settings)}件")


async def save_user_setting(user_id: int, settings: VoiceSettings):
    """ユーザー設定を1件DBに保存"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings
                (user_id, speaker_id, speed, pitch, intonation, volume)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id) DO UPDATE SET
                speaker_id = $2, speed = $3, pitch = $4, intonation = $5, volume = $6
            """,
            user_id,
            settings.speaker_id,
            settings.speed,
            settings.pitch,
            settings.intonation,
            settings.volume,
        )


async def load_guild_dicts():
    """DBからギルドの辞書設定をメモリにロード"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, word, reading FROM guild_dicts")
    guild_dicts.clear()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_dicts:
            guild_dicts[gid] = {}
        guild_dicts[gid][row["word"]] = row["reading"]
    logger.info(f"辞書設定を読み込みました: {len(guild_dicts)}ギルド")


async def add_dict_entry(guild_id: int, word: str, reading: str):
    """辞書エントリを1件DBに保存"""
    async with db_pool.acquire() as conn:
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
    """辞書エントリを1件DBから削除"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_dicts WHERE guild_id = $1 AND word = $2",
            guild_id,
            word,
        )


def apply_dict(guild_id: int, text: str) -> str:
    """テキストに辞書の置換を適用する"""
    d = guild_dicts.get(guild_id, {})
    for word, reading in d.items():
        text = text.replace(word, reading)
    return text


async def load_guild_mutes():
    """DBからギルドのミュート設定をメモリにロード"""
    async with db_pool.acquire() as conn:
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
    async with db_pool.acquire() as conn:
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
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_mutes WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )


def is_muted(guild_id: int, user_id: int) -> bool:
    """ユーザーがミュートされているか"""
    return user_id in guild_mutes.get(guild_id, set())


def clean_text(text: str) -> str:
    """読み上げ用にテキストを前処理する"""
    text = URL_PATTERN.sub("URLしょうりゃく", text)
    text = EMAIL_PATTERN.sub("メールアドレスしょうりゃく", text)
    text = CUSTOM_EMOJI_PATTERN.sub(r"\1", text)  # カスタム絵文字は名前だけ残す
    return text.strip()


# --- TTS エンジン ---


async def fetch_speakers():
    """全エンジンからスピーカー一覧を取得して統合キャッシュ"""
    speakers_cache.clear()
    speaker_engine.clear()
    characters.clear()

    async with aiohttp.ClientSession() as session:
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


def get_user_settings(user_id: int) -> VoiceSettings:
    """ユーザーの音声設定を返す"""
    return user_settings.get(user_id, VoiceSettings())


async def synthesize(text: str, settings: VoiceSettings) -> bytes:
    """エンジンでテキストを音声合成してwavバイトを返す"""
    engine_url, real_id = speaker_engine.get(
        settings.speaker_id, (ENGINES[0][1], settings.speaker_id)
    )
    async with aiohttp.ClientSession() as session:
        params = {"text": text, "speaker": real_id}
        async with session.post(f"{engine_url}/audio_query", params=params) as resp:
            resp.raise_for_status()
            query = await resp.json()

        # ユーザーの音声パラメータを適用
        query["speedScale"] = settings.speed
        query["pitchScale"] = settings.pitch
        query["intonationScale"] = settings.intonation
        query["volumeScale"] = settings.volume

        async with session.post(
            f"{engine_url}/synthesis",
            params={"speaker": real_id},
            json=query,
            headers={"Content-Type": "application/json"},
        ) as resp:
            resp.raise_for_status()
            return await resp.read()


async def play_next(guild_id: int, vc: discord.VoiceClient):
    """キューから次の音声を再生する"""
    queue = queues.get(guild_id, deque())
    if not queue:
        return

    audio_data = queue.popleft()
    audio_buffer = io.BytesIO(audio_data)

    source = discord.FFmpegPCMAudio(audio_buffer, pipe=True)

    def after_play(error):
        if error:
            logger.error(f"再生エラー: {error}")
        asyncio.run_coroutine_threadsafe(play_next(guild_id, vc), client.loop)

    vc.play(source, after=after_play)


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

        if self.guild_id not in guild_dicts:
            guild_dicts[self.guild_id] = {}
        guild_dicts[self.guild_id][word] = reading
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

        del d[word]
        if not d:
            guild_dicts.pop(self.guild_id, None)
        await delete_dict_entry(self.guild_id, word)

        content, view = build_dict_message(self.guild_id)
        await interaction.response.edit_message(content=content, view=view)


# --- イベント・コマンド ---


@client.event
async def on_ready():
    await init_db()
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


@tree.command(name="join", description="ボイスチャンネルに接続")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("先にボイスチャンネルに入ってください")
        return

    channel = interaction.user.voice.channel

    perms = channel.permissions_for(interaction.guild.me)
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

    try:
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
    except Exception as e:
        await interaction.response.send_message(f"VCへの接続に失敗しました: {e}")
        return

    queues[interaction.guild.id] = deque()
    read_channels[interaction.guild.id] = interaction.channel_id

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
        settings = get_user_settings(interaction.user.id)
        audio_data = await synthesize("せつぞくしました", settings)
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            queues[interaction.guild.id].append(audio_data)
            if not vc.is_playing() and not vc.is_paused():
                await play_next(interaction.guild.id, vc)
    except Exception as e:
        logger.error(f"接続挨拶の音声合成エラー: {e}")


@client.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    vc = member.guild.voice_client
    if not vc or not vc.is_connected():
        return

    guild_id = member.guild.id
    bot_channel = vc.channel

    # Bot以外のメンバーがいなくなったら自動切断
    members = [m for m in bot_channel.members if not m.bot]
    if not members:
        await vc.disconnect()
        queues.pop(guild_id, None)
        read_channels.pop(guild_id, None)
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
            settings = get_user_settings(member.id)
            audio_data = await synthesize(text, settings)
            if guild_id not in queues:
                queues[guild_id] = deque()
            queues[guild_id].append(audio_data)
            if not vc.is_playing() and not vc.is_paused():
                await play_next(guild_id, vc)
        except Exception as e:
            logger.error(f"入退室通知の音声合成エラー: {e}")


@tree.command(name="leave", description="ボイスチャンネルから切断")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        queues.pop(interaction.guild.id, None)
        read_channels.pop(interaction.guild.id, None)
        await interaction.response.send_message("切断しました")
    else:
        await interaction.response.send_message("ボイスチャンネルに接続していません")


@tree.command(name="vc", description="VCに接続/切断をトグル")
async def vc_toggle(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        queues.pop(interaction.guild.id, None)
        read_channels.pop(interaction.guild.id, None)
        await interaction.response.send_message("切断しました")
    else:
        await join.callback(interaction)


@tree.command(name="skip", description="現在読み上げ中の音声をスキップ")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc or not vc.is_playing():
        await interaction.response.send_message("再生中の音声はありません")
        return
    vc.stop()
    await interaction.response.send_message("スキップしました")


@tree.command(name="mute", description="指定ユーザーの読み上げをミュート")
@app_commands.describe(user="ミュートするユーザー")
async def mute_cmd(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        await interaction.response.send_message("Botはミュートできません")
        return
    await add_mute(interaction.guild.id, user.id)
    await interaction.response.send_message(f"{user.display_name} をミュートしました")


@tree.command(name="unmute", description="指定ユーザーのミュートを解除")
@app_commands.describe(user="ミュート解除するユーザー")
async def unmute_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_muted(interaction.guild.id, user.id):
        await interaction.response.send_message(
            f"{user.display_name} はミュートされていません"
        )
        return
    await remove_mute(interaction.guild.id, user.id)
    await interaction.response.send_message(
        f"{user.display_name} のミュートを解除しました"
    )


@tree.command(name="showmute", description="ミュート中のユーザー一覧")
async def showmute_cmd(interaction: discord.Interaction):
    mutes = guild_mutes.get(interaction.guild.id, set())
    if not mutes:
        await interaction.response.send_message("ミュート中のユーザーはいません")
        return
    lines = []
    for uid in mutes:
        member = interaction.guild.get_member(uid)
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
    if not characters:
        await interaction.response.send_message(
            "スピーカー情報がまだ読み込まれていません"
        )
        return

    # キャラクター名で検索
    matched_char = None
    for char_name in characters:
        if character.lower() in char_name.lower():
            matched_char = char_name
            if character.lower() == char_name.lower():
                break

    if not matched_char:
        await interaction.response.send_message(
            f"「{character}」に一致するキャラクターが見つかりません"
        )
        return

    # スタイル名で検索
    styles = characters[matched_char]
    matched_style = None
    for global_id, style_name in styles:
        if style.lower() in style_name.lower():
            matched_style = (global_id, style_name)
            if style.lower() == style_name.lower():
                break

    if not matched_style:
        style_names = ", ".join(s[1] for s in styles)
        await interaction.response.send_message(
            f"「{matched_char}」にスタイル「{style}」がありません\n"
            f"利用可能: {style_names}"
        )
        return

    speaker_id = matched_style[0]
    settings = get_user_settings(interaction.user.id)
    settings = VoiceSettings(
        speaker_id=speaker_id,
        speed=settings.speed,
        pitch=settings.pitch,
        intonation=settings.intonation,
        volume=settings.volume,
    )
    user_settings[interaction.user.id] = settings
    await save_user_setting(interaction.user.id, settings)
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
    for opt in interaction.data.get("options", []):
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
    settings = get_user_settings(interaction.user.id)

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
    user_settings[interaction.user.id] = new_settings
    await save_user_setting(interaction.user.id, new_settings)

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
    content, view = build_dict_message(interaction.guild.id)
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

    # 辞書で置換
    text = apply_dict(message.guild.id, text)

    # 長すぎるメッセージは切り詰め
    if len(text) > MAX_READ_LENGTH:
        text = text[:MAX_READ_LENGTH] + "、いかしょうりゃく"

    try:
        settings = get_user_settings(message.author.id)
        audio_data = await synthesize(text, settings)
    except aiohttp.ClientError:
        logger.warning("音声合成エンジンに接続できません（再起動中の可能性）")
        await message.channel.send(
            "音声エンジンに接続できません。しばらくお待ちください。"
        )
        return
    except Exception as e:
        logger.error(f"音声合成エラー: {e}")
        return

    guild_id = message.guild.id
    if guild_id not in queues:
        queues[guild_id] = deque()

    queues[guild_id].append(audio_data)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(guild_id, vc)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is required")
    client.run(DISCORD_TOKEN)
