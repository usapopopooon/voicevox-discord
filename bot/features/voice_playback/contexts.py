"""voice-playback feature 用の context protocol。

これらの protocol は、playback feature が composition root へ要求する
runtime surface を正確に文書化する。将来の移植で Python の module mutation を
引き継がずに済むよう、TypeScript の ``interface`` に近い形に寄せている。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import MutableMapping
from typing import Any, Protocol

import discord


class PlaybackStateContext(Protocol):
    """Bot 本体に依存しない queue 管理が必要とする runtime state。"""

    # Protocol は「実装を持たない interface」として読む。ここに列挙された
    # 属性だけが voice_playback.application から必要とされる runtime surface。
    # 追加したくなったら、まず「本当に playback feature の責務か」を確認する。
    QUEUE_MAXLEN: int
    queues: MutableMapping[int, deque[bytes]]
    read_channels: MutableMapping[int, int]
    play_locks: MutableMapping[int, asyncio.Lock]
    synth_order_locks: MutableMapping[int, asyncio.Lock]
    engine_error_notified_at: MutableMapping[int, float]


class DiscordPlaybackContext(PlaybackStateContext, Protocol):
    """Discord playback adapter が必要とする runtime surface。"""

    # discord.py / asyncio / subprocess は Python ランタイム固有なので
    # Any のままにする。TypeScript 化時はここを Discord.js client や
    # timer/scheduler interface に差し替える。
    asyncio: Any
    client: discord.Client
    discord: Any
    logger: logging.Logger
    subprocess: Any

    def can_start_playback(self, vc: discord.VoiceClient) -> bool:
        """voice client が新しい audio source を開始できるかを返す。"""
        ...

    def is_voice_client_connected(self, vc: discord.VoiceClient) -> bool:
        """voice client がまだ接続中かを返す。"""
        ...

    def make_playback_audio_source(self, audio_data: bytes) -> discord.AudioSource:
        """音声 bytes を Discord 互換 source へ変換する。"""
        ...

    async def play_next(self, guild_id: int, vc: discord.VoiceClient) -> None:
        """現在の source 終了後に再生を続ける。"""
        ...
