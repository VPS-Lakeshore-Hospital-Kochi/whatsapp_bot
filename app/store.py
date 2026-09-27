"""Small key-value store with TTL for sessions, dedupe and rate limits.

Use Redis in production (set REDIS_URL) so state survives restarts and is shared across workers.
"""

import json
import time
from typing import Any


class Store:
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def set_if_absent(self, key: str, ttl_seconds: int) -> bool: ...
    async def incr(self, key: str, ttl_seconds: int) -> int: ...


class MemoryStore(Store):
    def __init__(self):
        self._data: dict[str, tuple[Any, float | None]] = {}

    def _live(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires < time.time():
            del self._data[key]
            return None
        return item

    async def get(self, key):
        item = self._live(key)
        return None if item is None else item[0]

    async def set(self, key, value, ttl_seconds=None):
        self._data[key] = (value, time.time() + ttl_seconds if ttl_seconds else None)

    async def delete(self, key):
        self._data.pop(key, None)

    async def set_if_absent(self, key, ttl_seconds):
        if self._live(key) is not None:
            return False
        await self.set(key, 1, ttl_seconds)
        return True

    async def incr(self, key, ttl_seconds):
        item = self._live(key)
        if item is None:
            await self.set(key, 1, ttl_seconds)
            return 1
        value, expires = item
        self._data[key] = (value + 1, expires)
        return value + 1


class RedisStore(Store):
    def __init__(self, url: str):
        import redis.asyncio as redis

        self._r = redis.from_url(url, decode_responses=True)

    async def get(self, key):
        raw = await self._r.get(key)
        return None if raw is None else json.loads(raw)

    async def set(self, key, value, ttl_seconds=None):
        await self._r.set(key, json.dumps(value), ex=ttl_seconds)

    async def delete(self, key):
        await self._r.delete(key)

    async def set_if_absent(self, key, ttl_seconds):
        return bool(await self._r.set(key, "1", ex=ttl_seconds, nx=True))

    async def incr(self, key, ttl_seconds):
        pipe = self._r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count)


def make_store(redis_url: str) -> Store:
    return RedisStore(redis_url) if redis_url else MemoryStore()
