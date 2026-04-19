#!/usr/bin/env python3
"""Create active_voice_sessions table.

Tracks which voice channel each guild's bot is currently joined to,
so the bot can rejoin after a process restart or unexpected disconnect.

Usage:
  DATABASE_URL=... python bot/migrations/20260420_active_voice_sessions.py

Idempotent (CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

import asyncio
import os

import asyncpg


async def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_voice_sessions (
                guild_id BIGINT PRIMARY KEY,
                voice_channel_id BIGINT NOT NULL,
                text_channel_id BIGINT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
