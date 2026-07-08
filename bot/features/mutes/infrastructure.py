"""読み上げミュート状態の database access。"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


async def ensure_schema(conn: Any) -> None:
    """mute table がなければ作成する。"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_mutes (
            guild_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
    """)


async def load_guild_mutes(
    pool: Any,
    guild_mutes: MutableMapping[int, set[int]],
    *,
    logger: Any,
) -> None:
    """すべての guild の mute list を読み込む。"""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, user_id FROM guild_mutes")
    guild_mutes.clear()
    for row in rows:
        gid = row["guild_id"]
        if gid not in guild_mutes:
            guild_mutes[gid] = set()
        guild_mutes[gid].add(row["user_id"])
    logger.info(
        f"ミュート設定を読み込みました: "
        f"{sum(len(users) for users in guild_mutes.values())}件"
    )


async def add_mute(
    pool: Any,
    guild_mutes: MutableMapping[int, set[int]],
    *,
    guild_id: int,
    user_id: int,
) -> None:
    """ミュート対象ユーザー 1 人をメモリと database へ追加する。"""
    if guild_id not in guild_mutes:
        guild_mutes[guild_id] = set()
    guild_mutes[guild_id].add(user_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO guild_mutes (guild_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            guild_id,
            user_id,
        )


async def remove_mute(
    pool: Any,
    guild_mutes: MutableMapping[int, set[int]],
    *,
    guild_id: int,
    user_id: int,
) -> None:
    """ミュート対象ユーザー 1 人をメモリと database から削除する。"""
    mutes = guild_mutes.get(guild_id, set())
    mutes.discard(user_id)
    if not mutes:
        guild_mutes.pop(guild_id, None)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM guild_mutes WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
