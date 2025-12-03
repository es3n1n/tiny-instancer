from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from redis import asyncio as redis_lib
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from .config import config


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


redis = redis_lib.from_url(
    config.cache_connection_url,
    encoding='utf-8',
    decode_responses=True,
    retry_on_timeout=True,
    retry=Retry(ExponentialBackoff(cap=1), 3),
)


@asynccontextmanager
async def instance_lock(challenge: str, team_id: str) -> AsyncGenerator[bool]:
    lock = redis.lock(
        f'{config.PREFIX}:locks:instance:{challenge}:{team_id}',
        timeout=config.REDIS_LOCK_TIMEOUT_SECONDS,
        blocking_timeout=config.REDIS_LOCK_BLOCKING_TIMEOUT_SECONDS,
    )

    acquired = await lock.acquire(blocking=True)
    try:
        yield acquired
    finally:
        await lock.release()
