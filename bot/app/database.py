"""application layer の DB pool 配線。

この module は PostgreSQL connection pool を所有する。table SQL は、その data を
所有する feature 側に置く。
"""

from __future__ import annotations

from typing import Any

from features.dictionary import infrastructure as dictionary_infrastructure
from features.mutes import infrastructure as mutes_infrastructure
from features.voice_sessions import infrastructure as voice_sessions_infrastructure
from features.voice_settings import infrastructure as voice_settings_infrastructure


def require_pool(pool: Any | None) -> Any:
    """初期化済み asyncpg pool を返す。

    引数:
        pool: runtime が保持している pool 値。

    例外:
        RuntimeError: pool がまだ初期化されていない場合。
    """
    if pool is None:
        raise RuntimeError("DB接続プールが未初期化です（on_ready完了前の可能性）")
    return pool


async def init(ctx: Any) -> None:
    """DB pool を作成し、各 feature に自分の schema を保証させる。"""
    if ctx.db_pool is not None:
        return

    async with ctx.db_init_lock:
        # on_ready が再接続などで複数回呼ばれても、pool は一つだけ作る。
        # lock 取得後に再確認するのは、待っている間に別 coroutine が初期化を
        # 完了している可能性があるため。
        if ctx.db_pool is not None:
            return

        for attempt in range(5):
            try:
                ctx.db_pool = await ctx.asyncpg.create_pool(
                    ctx.DATABASE_URL,
                    min_size=ctx.DB_POOL_MIN_SIZE,
                    max_size=ctx.DB_POOL_MAX_SIZE,
                )
                break
            except (OSError, ctx.asyncpg.PostgresError) as e:
                if attempt < 4:
                    # Compose 起動直後は PostgreSQL がまだ accept していないことがある。
                    # Bot 全体を即死させず、短い retry で吸収する。
                    ctx.logger.warning(
                        f"DB接続失敗 ({attempt + 1}/5): {e}、2秒後にリトライ"
                    )
                    await ctx.asyncio.sleep(2)
                else:
                    raise

        assert ctx.db_pool is not None
        async with ctx.db_pool.acquire() as conn:
            await voice_settings_infrastructure.ensure_schema(conn)
            await dictionary_infrastructure.ensure_schema(conn)
            await mutes_infrastructure.ensure_schema(conn)
            await voice_sessions_infrastructure.ensure_schema(conn)
        ctx.logger.info("DB初期化完了")
