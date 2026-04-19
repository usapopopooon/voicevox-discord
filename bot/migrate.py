#!/usr/bin/env python3
"""Run DB migrations in bot/migrations in filename order.

Each migration file is a standalone executable Python script.
Applied migrations are tracked in `schema_migrations`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
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


async def _connect_with_retry(
    database_url: str,
    *,
    max_attempts: int = 5,
    interval_seconds: float = 2.0,
    logger: logging.Logger | None = None,
) -> asyncpg.Connection:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncpg.connect(database_url)
        except (OSError, asyncpg.PostgresError) as e:
            last_error = e
            if attempt < max_attempts:
                if logger is not None:
                    logger.warning(
                        "Migration DB connect failed (%d/%d): %s; retrying in %.1fs",
                        attempt,
                        max_attempts,
                        e,
                        interval_seconds,
                    )
                else:
                    print(
                        "Migration DB connect failed "
                        f"({attempt}/{max_attempts}): {e}; "
                        f"retrying in {interval_seconds:.1f}s"
                    )
                await asyncio.sleep(interval_seconds)
            else:
                break
    assert last_error is not None
    raise last_error


async def run_pending_migrations(
    database_url: str, *, logger: logging.Logger | None = None
) -> None:
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    conn = await _connect_with_retry(database_url, logger=logger)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        await _ensure_schema_table(conn)

        applied_rows = await conn.fetch("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in applied_rows}

        files = _migration_files()
        if not files:
            if logger is not None:
                logger.info("No migrations found.")
            else:
                print("No migrations found.")
            return

        for path in files:
            name = path.name
            if name in applied:
                if logger is not None:
                    logger.info("Skip migration: %s", name)
                else:
                    print(f"Skip migration: {name}")
                continue

            if logger is not None:
                logger.info("Apply migration: %s", name)
            else:
                print(f"Apply migration: {name}")
            started_at = time.monotonic()
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
            elapsed = time.monotonic() - started_at
            if logger is not None:
                logger.info("Applied migration: %s (%.2fs)", name, elapsed)
            else:
                print(f"Applied migration: {name} ({elapsed:.2f}s)")
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
        finally:
            await conn.close()


if __name__ == "__main__":
    db_url = os.getenv("DATABASE_URL")
    asyncio.run(run_pending_migrations(db_url or ""))
