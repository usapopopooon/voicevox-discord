"""読み上げミュート状態の application helper。"""

from __future__ import annotations

from collections.abc import Mapping


def is_muted(guild_mutes: Mapping[int, set[int]], guild_id: int, user_id: int) -> bool:
    """guild 内でユーザーが読み上げミュート中かを返す。"""
    return user_id in guild_mutes.get(guild_id, set())
