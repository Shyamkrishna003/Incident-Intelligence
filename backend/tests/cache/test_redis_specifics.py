"""Details only the real Redis implementations have: expiry (TTL) and stored formats."""

import uuid

import pytest
from redis.asyncio import Redis

from incident_intel.cache.api_keys import CachedApiKey, RedisApiKeyCache
from incident_intel.cache.gateway import RedisGateway
from incident_intel.cache.idempotency import RedisIdempotencyStore
from incident_intel.cache.rate_limit import RedisRateLimiter
from tests.cache.conftest import Clock

pytestmark = pytest.mark.redis

HASH = "a" * 64


def _entry() -> CachedApiKey:
    return CachedApiKey(
        api_key_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="ci",
        key_prefix="abcdefghijkl",
        key_hash="f" * 64,
        scopes=("ingest:write",),
        expires_at=None,
    )


async def test_rate_limit_counters_expire(
    redis_gateway: RedisGateway, redis_client: Redis, clock: Clock
) -> None:
    limiter = RedisRateLimiter(
        redis_gateway, limit=5, window_seconds=60, fail_open=True, clock=clock
    )

    await limiter.check("key-1")

    [key] = [k async for k in redis_client.scan_iter(match=redis_gateway.key("rl", "*"))]
    assert 0 < await redis_client.ttl(key) <= 120


async def test_idempotency_claim_expires_and_publish_keeps_the_ttl(
    redis_gateway: RedisGateway, redis_client: Redis
) -> None:
    store = RedisIdempotencyStore(redis_gateway, ttl_seconds=3600)
    project = uuid.uuid4()
    key = redis_gateway.key("idem", project, "k")

    await store.claim(project, "k", HASH)
    assert await redis_client.get(key) == f"{HASH}:pending"
    await redis_client.expire(key, 100)  # pretend most of the hour has passed

    await store.mark_published(project, "k", HASH)

    assert await redis_client.get(key) == f"{HASH}:published"
    assert 0 < await redis_client.ttl(key) <= 100  # not reset to an hour


async def test_api_key_entry_expires_and_never_contains_a_plaintext_key(
    redis_gateway: RedisGateway, redis_client: Redis
) -> None:
    cache = RedisApiKeyCache(redis_gateway, ttl_seconds=60)
    entry = _entry()

    await cache.put(entry)

    key = redis_gateway.key("apikey", entry.key_prefix)
    assert 0 < await redis_client.ttl(key) <= 60
    stored = await redis_client.get(key)
    assert stored is not None
    assert "ii_" not in stored


async def test_zero_ttl_disables_the_api_key_cache(
    redis_gateway: RedisGateway, redis_client: Redis
) -> None:
    cache = RedisApiKeyCache(redis_gateway, ttl_seconds=0)
    entry = _entry()

    await cache.put(entry)

    assert await cache.get(entry.key_prefix) is None
    assert [k async for k in redis_client.scan_iter(match=redis_gateway.key("apikey", "*"))] == []


async def test_unreadable_api_key_entry_is_treated_as_a_miss(
    redis_gateway: RedisGateway, redis_client: Redis
) -> None:
    cache = RedisApiKeyCache(redis_gateway, ttl_seconds=60)
    await redis_client.set(redis_gateway.key("apikey", "abcdefghijkl"), '{"old": "format"}')

    assert await cache.get("abcdefghijkl") is None


async def test_ping_reports_a_reachable_server(redis_gateway: RedisGateway) -> None:
    assert await redis_gateway.ping() is True
