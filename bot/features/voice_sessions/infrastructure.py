"""有効な voice-channel session の database access。"""

from __future__ import annotations

from typing import Any


async def ensure_schema(conn: Any) -> None:
    """active voice session table がなければ作成する。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_voice_sessions (
            guild_id BIGINT PRIMARY KEY,
            voice_channel_id BIGINT NOT NULL,
            text_channel_id BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


async def record_voice_session(
    pool: Any, *, guild_id: int, voice_channel_id: int, text_channel_id: int
) -> None:
    """restart と reconnect 復旧のために有効な VC session を保存する。"""
    async with pool.acquire() as conn:
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


async def forget_voice_session(pool: Any, *, guild_id: int) -> None:
    """意図的な切断後に有効な VC session を 1 件削除する。"""
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM active_voice_sessions WHERE guild_id = $1",
            guild_id,
        )


async def load_voice_sessions(pool: Any) -> list[tuple[int, int, int]]:
    """有効な VC session を ``(guild, voice_channel, text_channel)`` として返す。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, voice_channel_id, text_channel_id "
            "FROM active_voice_sessions"
        )
    return [
        (row["guild_id"], row["voice_channel_id"], row["text_channel_id"])
        for row in rows
    ]
