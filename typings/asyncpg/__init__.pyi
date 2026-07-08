"""この project で使う asyncpg API の最小型 stub。"""

from collections.abc import AsyncContextManager, Mapping, Sequence
from typing import Any, Protocol

class PostgresError(Exception):
    """PostgreSQL 操作で asyncpg が送出する基底例外。"""

class Record(Mapping[str, Any]):
    """asyncpg が query 結果として返す行 object。"""

class Connection(Protocol):
    """この project が使う asyncpg connection の最小 surface。"""

    async def execute(
        self, query: str, *args: object, timeout: float | None = ...
    ) -> str: ...
    async def executemany(
        self,
        command: str,
        args: Sequence[Sequence[object]],
        *,
        timeout: float | None = ...,
    ) -> None: ...
    async def fetch(
        self, query: str, *args: object, timeout: float | None = ...
    ) -> list[Record]: ...
    async def fetchval(
        self,
        query: str,
        *args: object,
        column: int = ...,
        timeout: float | None = ...,
    ) -> Any: ...
    async def close(self, *, timeout: float | None = ...) -> None: ...

class Pool(Protocol):
    """この project が使う asyncpg pool の最小 surface。"""

    def acquire(
        self, *, timeout: float | None = ...
    ) -> AsyncContextManager[Connection]: ...
    async def close(self) -> None: ...

async def connect(dsn: str | None = ..., **connect_kwargs: Any) -> Connection:
    """PostgreSQL へ接続する。"""
    ...

async def create_pool(
    dsn: str | None = ...,
    *,
    min_size: int = ...,
    max_size: int = ...,
    **connect_kwargs: Any,
) -> Pool:
    """PostgreSQL connection pool を作成する。"""
    ...
