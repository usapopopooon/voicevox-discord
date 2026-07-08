"""user voice-settings feature の database access。"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any, TypeVar

VoiceSettingsT = TypeVar("VoiceSettingsT")


async def ensure_schema(conn: Any) -> None:
    """user-settings table を作成し、必要な migration を行う。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            guild_id BIGINT NOT NULL DEFAULT 0,
            user_id BIGINT NOT NULL,
            speaker_id INTEGER NOT NULL DEFAULT 46,
            speed REAL NOT NULL DEFAULT 1.0,
            pitch REAL NOT NULL DEFAULT 0.0,
            intonation REAL NOT NULL DEFAULT 1.0,
            volume REAL NOT NULL DEFAULT 1.0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    await conn.execute(
        "ALTER TABLE user_settings "
        "ADD COLUMN IF NOT EXISTS guild_id BIGINT NOT NULL DEFAULT 0"
    )

    pk_cols = await conn.fetch(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.table_name = 'user_settings'
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """
    )
    current_pk = [row["column_name"] for row in pk_cols]
    if current_pk != ["guild_id", "user_id"]:
        pk_name = await conn.fetchval(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            WHERE tc.table_name = 'user_settings'
              AND tc.constraint_type = 'PRIMARY KEY'
            """
        )
        if pk_name:
            escaped_pk_name = pk_name.replace('"', '""')
            await conn.execute(
                f'ALTER TABLE user_settings DROP CONSTRAINT "{escaped_pk_name}"'
            )
        await conn.execute(
            "ALTER TABLE user_settings ADD PRIMARY KEY (guild_id, user_id)"
        )


async def load_user_settings(
    pool: Any,
    user_settings: MutableMapping[tuple[int, int], VoiceSettingsT],
    *,
    settings_factory: Callable[..., VoiceSettingsT],
    logger: Any,
) -> None:
    """すべてのユーザー音声設定をメモリへ読み込む。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT guild_id, user_id, speaker_id, speed, pitch, intonation, volume "
            "FROM user_settings"
        )
    user_settings.clear()
    for row in rows:
        user_settings[(row["guild_id"], row["user_id"])] = settings_factory(
            speaker_id=row["speaker_id"],
            speed=row["speed"],
            pitch=row["pitch"],
            intonation=row["intonation"],
            volume=row["volume"],
        )
    logger.info(f"ユーザー設定を読み込みました: {len(user_settings)}件")


async def save_user_setting(
    pool: Any,
    *,
    guild_id: int,
    user_id: int,
    settings: Any,
) -> None:
    """ユーザー 1 人分の音声設定を保存する。"""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_settings
                (guild_id, user_id, speaker_id, speed, pitch, intonation, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                speaker_id = $3, speed = $4, pitch = $5, intonation = $6, volume = $7
            """,
            guild_id,
            user_id,
            settings.speaker_id,
            settings.speed,
            settings.pitch,
            settings.intonation,
            settings.volume,
        )
