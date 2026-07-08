"""読み上げ feature 用の Discord message adapter。"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

import aiohttp
import discord


def attachment_category(content_type: str | None) -> str:
    """添付ファイル MIME type に対応する日本語読み上げカテゴリを返す。"""
    content_type = (content_type or "").lower()
    if content_type.startswith("image/"):
        return "がぞう"
    if content_type.startswith("video/"):
        return "どうが"
    if content_type.startswith("audio/"):
        return "おんせい"
    if content_type == "application/pdf":
        return "ぴーでぃーえふ"
    if content_type.startswith("text/"):
        return "てきすとふぁいる"
    if content_type in (
        "application/zip",
        "application/x-zip-compressed",
        "application/x-7z-compressed",
        "application/x-tar",
        "application/gzip",
    ):
        return "あっしゅくふぁいる"
    return "ふぁいる"


def build_attachment_notice(attachments: Sequence[discord.Attachment]) -> str:
    """Discord 添付ファイル一覧の読み上げ通知文を返す。"""
    seen: list[str] = []
    for attachment in attachments:
        category = attachment_category(attachment.content_type)
        if category not in seen:
            seen.append(category)
    if not seen:
        return ""
    return "と".join(seen) + "がてんぷされました"


async def on_message(ctx: Any, message: discord.Message) -> None:
    """読み上げ対象の Discord message 1 件を queue 済み TTS 音声へ変換する。"""
    if message.author.bot:
        return
    if not message.guild:
        return

    vc = ctx._as_voice_client(message.guild.voice_client)
    if vc is None or not vc.is_connected():
        return
    if ctx.read_channels.get(message.guild.id) != message.channel.id:
        return
    if ctx.is_muted(message.guild.id, message.author.id):
        return
    if message.content.startswith(";"):
        return

    trace_id = ctx._new_trace_id()
    text = ctx.clean_text(message.clean_content)
    attachment_notice = ctx._build_attachment_notice(message.attachments)
    if not text and not attachment_notice:
        return

    text = ctx.apply_dict(message.guild.id, text)
    if len(text) > ctx.MAX_READ_LENGTH:
        text = text[: ctx.MAX_READ_LENGTH] + "、いかりゃく"
    if attachment_notice:
        text = f"{text}、{attachment_notice}" if text else attachment_notice

    guild_id = message.guild.id
    existing_queue = ctx.queues.get(guild_id)
    if existing_queue is not None and len(existing_queue) >= ctx.QUEUE_MAXLEN:
        ctx._log_event(
            logging.INFO,
            "queue.drop.full",
            trace_id=trace_id,
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            queue_length=len(existing_queue),
        )
        return

    if not ctx._rate_limit_try_consume(guild_id, message.author.id):
        ctx._log_event(
            logging.INFO,
            "rate_limit.hit",
            trace_id=trace_id,
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
        )
        return

    try:
        settings = ctx.get_user_settings(guild_id, message.author.id)
        audio_data = await ctx.synthesize(text, settings)
        queue = ctx._ensure_queue(guild_id)
        queue.append(audio_data)
        ctx._log_event(
            logging.INFO,
            "queue.enqueue",
            trace_id=trace_id,
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            queue_length=len(queue),
            text_length=len(text),
            speaker_id=settings.speaker_id,
        )
    except aiohttp.ClientError:
        ctx._record_recent_error(
            "tts.engine_unavailable", "ClientError", trace_id, guild_id=guild_id
        )
        ctx.logger.warning("音声合成エンジンに接続できません（再起動中の可能性）")
        now = time.monotonic()
        last = ctx.engine_error_notified_at.get(guild_id, 0.0)
        if now - last >= ctx.ENGINE_ERROR_NOTIFY_INTERVAL:
            ctx.engine_error_notified_at[guild_id] = now
            await message.channel.send(
                "音声エンジンに接続できません。しばらくお待ちください。"
            )
        return
    except Exception as e:
        ctx._record_recent_error(
            "message.synthesize.failed", str(e), trace_id, guild_id=guild_id
        )
        ctx._log_event(
            logging.WARNING,
            "message.synthesize.failed",
            trace_id=trace_id,
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            error=str(e),
        )
        ctx.logger.error(f"音声合成エラー: {e}")
        return

    if ctx._can_start_playback(vc):
        await ctx.play_next(guild_id, vc)
