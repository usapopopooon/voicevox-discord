#!/usr/bin/env python3
"""Seed builtin_reading_dicts from current built-in dictionaries.

Usage:
  DATABASE_URL=... python bot/migrations/20260419_seed_builtin_reading_dicts.py

This migration is idempotent:
- Existing rows are preserved.
- Missing rows are inserted with ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg


async def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    # Ensure `import bot` resolves to bot/bot.py when run from repo root.
    script_dir = Path(__file__).resolve().parent
    bot_dir = script_dir.parent
    if str(bot_dir) not in sys.path:
        sys.path.insert(0, str(bot_dir))
    from bot import _DEFAULT_ENGLISH_WORD_READINGS, _DEFAULT_READING_CORRECTIONS

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS builtin_reading_dicts (
                dict_type TEXT NOT NULL,
                word TEXT NOT NULL,
                reading TEXT NOT NULL,
                PRIMARY KEY (dict_type, word),
                CHECK (dict_type IN ('jp', 'en'))
            )
            """
        )

        seed_rows = [
            ("jp", word, reading)
            for word, reading in _DEFAULT_READING_CORRECTIONS.items()
        ] + [
            ("en", word, reading)
            for word, reading in _DEFAULT_ENGLISH_WORD_READINGS.items()
        ]

        await conn.executemany(
            """
            INSERT INTO builtin_reading_dicts (dict_type, word, reading)
            VALUES ($1, $2, $3)
            ON CONFLICT (dict_type, word) DO NOTHING
            """,
            seed_rows,
        )

        print(
            "Seed migration completed: "
            f"jp={len(_DEFAULT_READING_CORRECTIONS)}, "
            f"en={len(_DEFAULT_ENGLISH_WORD_READINGS)}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
