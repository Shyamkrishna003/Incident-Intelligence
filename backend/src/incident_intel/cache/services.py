"""Wires the Redis-backed components together for the API and the admin CLI."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from incident_intel.cache.api_keys import ApiKeyCache, RedisApiKeyCache
from incident_intel.cache.gateway import RedisGateway
from incident_intel.cache.idempotency import IdempotencyStore, RedisIdempotencyStore
from incident_intel.cache.rate_limit import RateLimiter, RedisRateLimiter
from incident_intel.core.config import Settings


@dataclass(frozen=True)
class CacheServices:
    """Everything the application uses Redis for. Tests substitute in-memory versions."""

    rate_limiter: RateLimiter
    idempotency: IdempotencyStore
    api_keys: ApiKeyCache
    ping: Callable[[], Awaitable[bool]]
    close: Callable[[], Awaitable[None]]


def create_gateway(settings: Settings) -> RedisGateway:
    timeout = settings.redis_timeout_seconds
    client = Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        # No client-side retries: one short timeout, then the circuit breaker takes over.
        retry=Retry(NoBackoff(), 0),
    )
    return RedisGateway(client, key_prefix=settings.redis_key_prefix)


def build_redis_cache(settings: Settings) -> CacheServices:
    """Build the Redis-backed services. Connects lazily: Redis need not be up yet."""
    gateway = create_gateway(settings)
    return CacheServices(
        rate_limiter=RedisRateLimiter(
            gateway,
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            fail_open=settings.rate_limit_fail_open,
        ),
        idempotency=RedisIdempotencyStore(gateway, ttl_seconds=settings.idempotency_ttl_seconds),
        api_keys=RedisApiKeyCache(gateway, ttl_seconds=settings.api_key_cache_ttl_seconds),
        ping=gateway.ping,
        close=gateway.close,
    )
