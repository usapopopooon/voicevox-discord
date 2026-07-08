#!/usr/bin/env python3
"""database migration をファイル名順に実行する。

各 migration file は単独実行できる Python script として扱う。
適用済み migration は `schema_migrations` で管理する。

runner 自体は日付付き script と同じ ``bot/migrations`` に置くが、
migration 対象ではない。先頭が 4 桁の file だけを候補にすることで、
helper module、test、``__init__.py`` を実行対象から外す。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent
MIGRATION_FILE_PATTERN = "[0-9][0-9][0-9][0-9]*.py"
# application 内で安定して使う advisory lock key。値そのものに意味はなく、
# 複数 Bot process が migration を直列化できるよう固定されていればよい。
ADVISORY_LOCK_KEY = 775832991


class MigrationConnection(Protocol):
    """migration runner が使う小さな asyncpg connection surface。"""

    async def execute(
        self, query: str, *args: object, timeout: float | None = None
    ) -> str: ...

    async def fetch(
        self, query: str, *args: object, timeout: float | None = None
    ) -> list[Mapping[str, object]]: ...

    async def close(self, *, timeout: float | None = None) -> None: ...


async def _ensure_schema_table(conn: MigrationConnection) -> None:
    """migration 台帳 table がなければ作成する。

    引数:
        conn: migration runner が使う接続済み asyncpg connection。
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """日付付き migration script を決定的な順序で返す。

    引数:
        migrations_dir: migration script を含む directory。

    戻り値:
        ファイル名が 4 桁から始まる path の sorted list。
        ``runner.py`` などの helper module や test は意図的に除外する。
    """
    return sorted(migrations_dir.glob(MIGRATION_FILE_PATTERN))


async def _connect_with_retry(
    database_url: str,
    *,
    max_attempts: int = 5,
    interval_seconds: float = 2.0,
    logger: logging.Logger | None = None,
) -> MigrationConnection:
    """起動直後用の短い retry window 付きで PostgreSQL に接続する。

    引数:
        database_url: PostgreSQL 接続 URL。
        max_attempts: 例外を送出するまでの最大接続試行回数。
        interval_seconds: retry 可能な試行の間に待つ秒数。
        logger: 任意の logger。省略時は CLI 用に進捗を print する。

    戻り値:
        接続済み asyncpg connection。

    例外:
        OSError: network/socket layer の失敗が続いた場合。
        asyncpg.PostgresError: retry 後も PostgreSQL が接続を拒否する、または
            接続を提供できない場合。
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return cast(
                MigrationConnection,
                await asyncpg.connect(database_url),
            )
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
    """未適用の migration script をそれぞれ 1 回だけ適用する。

    引数:
        database_url: PostgreSQL connection URL。CLI mode でも必須。
        logger: Bot runtime が使う任意の logger。省略時は直接実行向けに
            進捗を print する。

    例外:
        RuntimeError: ``database_url`` が空、または migration subprocess が
            non-zero status で終了した場合。
        asyncpg.PostgresError: DB setup または台帳書き込みに失敗した場合。
    """
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    conn = await _connect_with_retry(database_url, logger=logger)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        await _ensure_schema_table(conn)

        applied_rows = await conn.fetch("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in applied_rows}

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
