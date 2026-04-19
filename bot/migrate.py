#!/usr/bin/env python3
"""Run DB migrations in bot/migrations in filename order.

Each migration file is a standalone executable Python script.
Applied migrations are tracked in `schema_migrations`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
ADVISORY_LOCK_KEY = 775832991  # arbitrary stable integer for this app


async def _ensure_schema_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.py"))
    return [p for p in files if p.name != "__init__.py"]


async def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        await _ensure_schema_table(conn)

        applied_rows = await conn.fetch("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in applied_rows}

        files = _migration_files()
        if not files:
            print("No migrations found.")
            return

        for path in files:
            name = path.name
            if name in applied:
                print(f"Skip migration: {name}")
                continue

            print(f"Apply migration: {name}")
            proc = subprocess.run(
                [sys.executable, str(path)],
                env=os.environ.copy(),
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Migration failed: {name}")

            await conn.execute(
                "INSERT INTO schema_migrations (name) "
                "VALUES ($1) ON CONFLICT DO NOTHING",
                name,
            )
            print(f"Applied migration: {name}")
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        finally:
            await conn.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
