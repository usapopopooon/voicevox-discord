"""voice-playback feature 用の Discord adapter。"""

from __future__ import annotations

import concurrent.futures
import io
import wave

import discord

from .contexts import DiscordPlaybackContext

# Discord が要求する PCM フォーマット: 48kHz stereo s16le、20ms = 3840B/frame
# この値を Discord adapter に閉じ込めることで、音声合成 feature は
# 「どのプラットフォームへ出すか」を知らなくてよい。
DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2
DISCORD_FRAME_SIZE = (
    DISCORD_SAMPLE_RATE * 20 // 1000 * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
)


def make_audio_source(
    ctx: DiscordPlaybackContext, audio_data: bytes
) -> discord.AudioSource:
    """合成済み WAV bytes から Discord AudioSource を作る。

    Discord 互換 PCM は ffmpeg process overhead を避けるため直接再生する。
    その他の形式は互換性のため ``FFmpegPCMAudio`` へ fallback する。
    """
    try:
        with wave.open(io.BytesIO(audio_data), "rb") as wav:
            if (
                wav.getnchannels() == DISCORD_CHANNELS
                and wav.getsampwidth() == DISCORD_SAMPLE_WIDTH
                and wav.getframerate() == DISCORD_SAMPLE_RATE
            ):
                # discord.PCMAudio は 20ms 単位で読む前提。端数があると末尾が
                # 欠けたりノイズになり得るため、無音で frame 境界まで埋める。
                pcm = wav.readframes(wav.getnframes())
                remainder = len(pcm) % DISCORD_FRAME_SIZE
                if remainder:
                    pcm += b"\x00" * (DISCORD_FRAME_SIZE - remainder)
                return ctx.discord.PCMAudio(io.BytesIO(pcm))
    except (wave.Error, EOFError, ValueError) as e:
        ctx.logger.debug(f"PCM直接再生不可、FFmpegにフォールバック: {e}")

    return ctx.discord.FFmpegPCMAudio(
        io.BytesIO(audio_data),
        pipe=True,
        before_options="-loglevel error",
        stderr=ctx.subprocess.DEVNULL,
    )


async def play_next(
    ctx: DiscordPlaybackContext, guild_id: int, vc: discord.VoiceClient
) -> None:
    """queue 済みの次の audio item を Discord voice client へ再生する。"""
    lock = ctx.play_locks.setdefault(guild_id, ctx.asyncio.Lock())
    async with lock:
        if not ctx.can_start_playback(vc):
            return

        queue = ctx.queues.get(guild_id)
        if not queue:
            return

        audio_data = queue.popleft()
        source = ctx.make_playback_audio_source(audio_data)

        def after_play(error: Exception | None) -> None:
            # discord.py の after callback は別スレッド側で呼ばれることがある。
            # await はできないので、client loop に coroutine を戻して次曲へ進める。
            if error:
                ctx.logger.error(f"再生エラー: {error}")
            future = ctx.asyncio.run_coroutine_threadsafe(
                ctx.play_next(guild_id, vc), ctx.client.loop
            )

            def _log_future_exception(fut: concurrent.futures.Future[None]) -> None:
                # run_coroutine_threadsafe の例外は result() まで表に出ない。
                # ここで拾わないと「次の再生が止まった理由」がログに残らない。
                try:
                    fut.result()
                except Exception as exc:
                    ctx.logger.error(f"次の再生でエラー: {exc}")

            future.add_done_callback(_log_future_exception)

        try:
            vc.play(source, after=after_play)
        except ctx.discord.ClientException as e:
            if ctx.is_voice_client_connected(vc):
                ctx.logger.warning(f"再生失敗、音声をキュー先頭に戻す: {e}")
                queue.appendleft(audio_data)
            else:
                ctx.logger.warning(f"再生スキップ（VC切断済み）、音声を破棄: {e}")
