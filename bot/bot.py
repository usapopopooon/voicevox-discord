import asyncio
import io
import logging
import os
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
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
DEFAULT_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER_ID", "3"))
DATABASE_URL = os.getenv("DATABASE_URL", "")

logger.info(f"VOICEVOX_URL: {VOICEVOX_URL}")
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
speakers_cache: dict[int, str] = {}
guild_dicts: dict[int, dict[str, str]] = {}

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


# --- VOICEVOX ---


async def fetch_speakers():
    """VOICEVOXからスピーカー一覧を取得してキャッシュ"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{VOICEVOX_URL}/speakers") as resp:
            resp.raise_for_status()
            data = await resp.json()

    cache = {}
    for speaker in data:
        name = speaker["name"]
        for style in speaker["styles"]:
            style_name = style["name"]
            style_id = style["id"]
            cache[style_id] = f"{name}（{style_name}）"

    speakers_cache.clear()
    speakers_cache.update(cache)
    logger.info(f"スピーカー一覧を取得しました: {len(speakers_cache)}件")


def get_user_settings(user_id: int) -> VoiceSettings:
    """ユーザーの音声設定を返す"""
    return user_settings.get(user_id, VoiceSettings())


async def synthesize(text: str, settings: VoiceSettings) -> bytes:
    """VOICEVOXでテキストを音声合成してwavバイトを返す"""
    async with aiohttp.ClientSession() as session:
        params = {"text": text, "speaker": settings.speaker_id}
        async with session.post(f"{VOICEVOX_URL}/audio_query", params=params) as resp:
            resp.raise_for_status()
            query = await resp.json()

        # ユーザーの音声パラメータを適用
        query["speedScale"] = settings.speed
        query["pitchScale"] = settings.pitch
        query["intonationScale"] = settings.intonation
        query["volumeScale"] = settings.volume

        async with session.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": settings.speaker_id},
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

    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()

    queues[interaction.guild.id] = deque()
    read_channels[interaction.guild.id] = interaction.channel_id
    await interaction.response.send_message(f"「{channel.name}」に接続しました")

    # 接続時に音声で挨拶
    try:
        settings = get_user_settings(interaction.user.id)
        audio_data = await synthesize("接続しました", settings)
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            queues[interaction.guild.id].append(audio_data)
            if not vc.is_playing() and not vc.is_paused():
                await play_next(interaction.guild.id, vc)
    except Exception as e:
        logger.error(f"接続挨拶の音声合成エラー: {e}")


@tree.command(name="leave", description="ボイスチャンネルから切断")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        queues.pop(interaction.guild.id, None)
        read_channels.pop(interaction.guild.id, None)
        await interaction.response.send_message("切断しました")
    else:
        await interaction.response.send_message("ボイスチャンネルに接続していません")


@tree.command(name="speaker", description="自分の読み上げキャラクターを変更")
@app_commands.describe(character="キャラクター名で検索")
async def speaker(interaction: discord.Interaction, character: str):
    try:
        speaker_id = int(character)
    except ValueError:
        await interaction.response.send_message("キャラクターの選択が無効です")
        return

    if speakers_cache and speaker_id not in speakers_cache:
        await interaction.response.send_message("存在しないキャラクターです")
        return

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
async def speaker_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not speakers_cache:
        return []
    choices = []
    for sid, name in speakers_cache.items():
        if current == "" or current.lower() in name.lower():
            choices.append(app_commands.Choice(name=name, value=str(sid)))
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

    text = message.clean_content.strip()
    if not text:
        return

    # 辞書で置換
    text = apply_dict(message.guild.id, text)

    # 長すぎるメッセージは切り詰め
    if len(text) > 100:
        text = text[:100] + "、以下省略"

    try:
        settings = get_user_settings(message.author.id)
        audio_data = await synthesize(text, settings)
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
