from typing import Any, Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings


class Enqueuer(Protocol):
    async def enqueue(self, function: str, *args: Any, **kwargs: Any) -> None: ...


class ArqEnqueuer:
    def __init__(self, redis_url: str) -> None:
        self._redis_url: str = redis_url
        self._pool: ArqRedis | None = None

    async def enqueue(self, function: str, *args: Any, **kwargs: Any) -> None:
        if self._pool is None:
            self._pool = await create_pool(RedisSettings.from_dsn(self._redis_url))
        _ = await self._pool.enqueue_job(function, *args, **kwargs)
