"""Per-subject request rate limiting (the subject is an API key id).

Algorithm: a fixed-window counter. Time is cut into windows of ``window_seconds``; each
subject has one counter per window, incremented on every request. Over ``limit`` → reject
until the window ends.

Trade-off: a client can send up to ``limit`` requests at the very end of one window and
``limit`` more at the start of the next (a 2x burst across the boundary). That is acceptable
for protecting the pipeline from floods; a token bucket would smooth it at the cost of a Lua
script.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from incident_intel.cache.gateway import CacheUnavailableError, RedisGateway


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    # When rejected: seconds until the client may retry.
    retry_after_seconds: int = 0
    # True when the limiter itself could not be reached (the decision is a fallback).
    limiter_unavailable: bool = False


class RateLimiter(Protocol):
    async def check(self, subject: str) -> RateLimitDecision: ...


def window_position(now: float, window_seconds: int) -> tuple[int, int]:
    """Return (index of the window containing ``now``, whole seconds until it ends)."""
    index = int(now // window_seconds)
    seconds_left = window_seconds - int(now % window_seconds)
    return index, seconds_left


class RedisRateLimiter:
    def __init__(
        self,
        gateway: RedisGateway,
        *,
        limit: int,
        window_seconds: int,
        fail_open: bool,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._gateway = gateway
        self._limit = limit
        self._window = window_seconds
        self._fail_open = fail_open
        self._clock = clock

    async def check(self, subject: str) -> RateLimitDecision:
        index, seconds_left = window_position(self._clock(), self._window)
        key = self._gateway.key("rl", subject, index)

        async def increment(client: Redis) -> int:
            # MULTI/EXEC: the counter and its expiry are set together. The window index is
            # part of the key, so old counters simply expire; nothing is ever reset.
            async with client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window * 2)
                count, _ = await pipe.execute()
            return int(count)

        try:
            count = await self._gateway.run(increment)
        except CacheUnavailableError:
            # Fail open (default): availability over strictness while Redis is down.
            return RateLimitDecision(
                allowed=self._fail_open, retry_after_seconds=0, limiter_unavailable=True
            )
        if count > self._limit:
            return RateLimitDecision(allowed=False, retry_after_seconds=seconds_left)
        return RateLimitDecision(allowed=True)
