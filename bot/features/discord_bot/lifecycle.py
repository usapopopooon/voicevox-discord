"""discord_bot feature の Discord lifecycle event handler。"""

from __future__ import annotations

from typing import Any

import discord


async def on_ready(ctx: Any) -> None:
    """Discord gateway login 後に runtime state を初期化する。"""
    if ctx.RUN_DB_MIGRATIONS and not ctx._migrations_ran:
        await ctx.migration_runner.run_pending_migrations(
            ctx.DATABASE_URL, logger=ctx.logger
        )
        ctx._migrations_ran = True
    await ctx.init_db()
    await ctx.load_builtin_reading_dicts()
    await ctx.load_user_settings()
    await ctx.load_guild_dicts()
    await ctx.load_guild_mutes()

    try:
        await ctx.tree.sync()
        ctx.logger.info("スラッシュコマンドを同期しました")
    except Exception as e:
        ctx.logger.warning(f"スラッシュコマンドの同期に失敗: {e}")

    user = ctx.client.user
    if user is not None:
        ctx.logger.info(f"Botログイン: {user} (ID: {user.id})")

    try:
        await ctx.client.change_presence(
            activity=discord.CustomActivity(name="読み上げ中")
        )
    except Exception as e:
        ctx.logger.warning(f"プレゼンス設定に失敗: {e}")

    try:
        await ctx.start_internal_tts_api()
    except Exception as e:
        ctx.logger.warning(f"内部TTS APIの起動に失敗: {e}")


async def on_guild_remove(ctx: Any, guild: discord.Guild) -> None:
    """Bot が guild から退出した後にインメモリ状態を解放する。"""
    guild_id = guild.id
    try:
        await ctx.forget_voice_session(guild_id)
    except Exception as e:
        ctx.logger.warning(f"VCセッション削除に失敗: {e}")
    ctx._cleanup_guild_state(guild_id)
    ctx.guild_dicts.pop(guild_id, None)
    ctx.guild_mutes.pop(guild_id, None)
    ctx._dict_patterns.pop(guild_id, None)

    stale_keys = [key for key in ctx.user_settings if key[0] == guild_id]
    for key in stale_keys:
        ctx.user_settings.pop(key, None)

    stale_buckets = [key for key in ctx._user_buckets if key[0] == guild_id]
    for key in stale_buckets:
        ctx._user_buckets.pop(key, None)

    ctx.logger.info(f"ギルド退出によりメモリ状態を解放 (Guild: {guild_id})")
