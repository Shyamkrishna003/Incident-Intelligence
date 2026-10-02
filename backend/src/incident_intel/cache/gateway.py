"""The single doorway to Redis, with a circuit breaker.

Every Redis call goes through ``RedisGateway.run``. Any failure is turned into
``CacheUnavailableError`` so callers handle exactly one error type, and the breaker then
"opens" for a short cooldown: during it, calls fail immediately instead of each request
waiting for another connection timeout. That keeps a Redis outage from adding latency to
every request.
"""

import time
from collections.abc import Awaitable, Callable

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)


class CacheUnavailableError(Exception):
    """Redis could not be used for this operation."""


def as_text(value: bytes | str | None) -> str | None:
    """Normalize a Redis reply to text (the client decodes, but its types allow bytes)."""
    return value.decode() if isinstance(value, bytes) else value


class RedisGateway:
    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str,
        cooldown_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._open_until = 0.0
        self._degraded = False

    def key(self, *parts: object) -> str:
        """Build a namespaced key, e.g. ``ii:rl:<api key id>:<window>``."""
        return self._key_prefix + ":".join(str(part) for part in parts)

    async def run[T](self, operation: Callable[[Redis], Awaitable[T]]) -> T:
        if self._clock() < self._open_until:
            raise CacheUnavailableError("circuit open")
        try:
            result = await operation(self._client)
        except (RedisError, OSError, TimeoutError) as exc:
            self._open_until = self._clock() + self._cooldown
            if not self._degraded:  # log the transition, not every failed call
                self._degraded = True
                logger.warning("redis_unavailable", error_type=type(exc).__name__)
            raise CacheUnavailableError(type(exc).__name__) from exc
        if self._degraded:
            self._degraded = False
            logger.info("redis_recovered")
        return result

    async def ping(self) -> bool:
        try:
            await self.run(lambda client: client.ping())
        except CacheUnavailableError:
            return False
        return True

    async def close(self) -> None:
        await self._client.aclose()
