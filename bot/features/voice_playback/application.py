"""Bot 本体に依存しない再生 queue と状態管理。"""

from __future__ import annotations

from collections import deque

from .contexts import PlaybackStateContext


def new_queue(ctx: PlaybackStateContext) -> deque[bytes]:
    """guild/server scope 1 件分の上限付き音声 queue を作る。"""
    return deque(maxlen=ctx.QUEUE_MAXLEN)


def ensure_queue(ctx: PlaybackStateContext, guild_id: int) -> deque[bytes]:
    """guild/server scope の queue を返し、なければ作成する。"""
    queue = ctx.queues.get(guild_id)
    if queue is None:
        # setdefault でも書けるが、明示的に分けると「新規作成時だけ bounded queue」
        # になることが読みやすい。TypeScript の Map 実装にも移しやすい形。
        queue = new_queue(ctx)
        ctx.queues[guild_id] = queue
    return queue


def cleanup_guild_state(ctx: PlaybackStateContext, guild_id: int) -> None:
    """guild/server scope 1 件分の再生状態をすべて消す。"""
    ctx.queues.pop(guild_id, None)
    ctx.read_channels.pop(guild_id, None)
    ctx.play_locks.pop(guild_id, None)
    ctx.synth_order_locks.pop(guild_id, None)
    ctx.engine_error_notified_at.pop(guild_id, None)


def cleanup_guild_playback_state(ctx: PlaybackStateContext, guild_id: int) -> None:
    """読み上げ対象を残したまま一時的な再生状態だけを消す。"""
    # Discord の一時切断・再接続では read_channels を残す。ここで消すと、
    # reconnect 後に「どのテキストチャンネルを読むか」を失ってしまう。
    ctx.queues.pop(guild_id, None)
    ctx.play_locks.pop(guild_id, None)
    ctx.synth_order_locks.pop(guild_id, None)
    ctx.engine_error_notified_at.pop(guild_id, None)
