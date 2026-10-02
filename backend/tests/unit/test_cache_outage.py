"""How the cache layer behaves when Redis is down. Needs no Redis: that is the point."""

import uuid
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from incident_intel.cache.api_keys import CachedApiKey
from incident_intel.cache.gateway import CacheUnavailableError, RedisGateway
from incident_intel.cache.idempotency import ClaimOutcome
from incident_intel.cache.rate_limit import window_position
from incident_intel.cache.services import build_redis_cache
from tests.support import make_settings

UNREACHABLE_DB = "postgresql+asyncpg://user:pw@127.0.0.1:1/unreachable_test"
# Nothing listens on port 1, so connections are refused immediately.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Calls:
    """Counts how often the gateway actually tried to talk to Redis."""

    def __init__(self, *, fail: bool) -> None:
        self.count = 0
        self.fail = fail

    async def __call__(self, _client: Redis) -> str:
        self.count += 1
        if self.fail:
            raise RedisConnectionError("down")
        return "ok"


def _gateway(clock: _Clock) -> RedisGateway:
    # The operations below never touch the client, so a placeholder is enough.
    return RedisGateway(cast(Redis, object()), key_prefix="t:", cooldown_seconds=5, clock=clock)


def test_keys_are_namespaced() -> None:
    assert _gateway(_Clock()).key("rl", "abc", 7) == "t:rl:abc:7"


async def test_failure_becomes_cache_unavailable() -> None:
    with pytest.raises(CacheUnavailableError):
        await _gateway(_Clock()).run(_Calls(fail=True))


async def test_open_circuit_skips_redis_until_the_cooldown_ends() -> None:
    clock = _Clock()
    gateway = _gateway(clock)
    failing = _Calls(fail=True)
    with pytest.raises(CacheUnavailableError):
        await gateway.run(failing)

    clock.now += 4.9
    with pytest.raises(CacheUnavailableError):
        await gateway.run(failing)
    assert failing.count == 1  # second call failed fast without trying Redis

    clock.now += 0.2  # cooldown over: Redis is tried again
    recovered = _Calls(fail=False)
    assert await gateway.run(recovered) == "ok"
    assert recovered.count == 1


async def test_errors_that_are_not_redis_failures_pass_through() -> None:
    clock = _Clock()
    gateway = _gateway(clock)

    async def bug(_client: Redis) -> str:
        raise ValueError("a bug, not an outage")

    with pytest.raises(ValueError, match="a bug"):
        await gateway.run(bug)
    # The circuit stayed closed: the next call reaches Redis.
    assert await gateway.run(_Calls(fail=False)) == "ok"


def test_window_position() -> None:
    assert window_position(120.0, 60) == (2, 60)
    assert window_position(179.9, 60) == (2, 1)
    assert window_position(180.0, 60) == (3, 60)


# --- The real Redis-backed services against an unreachable server -----------------------


async def test_rate_limiter_fails_open_by_default() -> None:
    cache = build_redis_cache(make_settings(UNREACHABLE_DB, redis_url=UNREACHABLE_REDIS))

    decision = await cache.rate_limiter.check("key-1")

    assert decision.allowed is True
    assert decision.limiter_unavailable is True
    await cache.close()


async def test_rate_limiter_can_fail_closed() -> None:
    cache = build_redis_cache(
        make_settings(UNREACHABLE_DB, redis_url=UNREACHABLE_REDIS, rate_limit_fail_open=False)
    )

    decision = await cache.rate_limiter.check("key-1")

    assert decision.allowed is False
    assert decision.limiter_unavailable is True
    await cache.close()


async def test_idempotency_store_reports_unavailable() -> None:
    cache = build_redis_cache(make_settings(UNREACHABLE_DB, redis_url=UNREACHABLE_REDIS))
    project = uuid.uuid4()

    assert await cache.idempotency.claim(project, "k", "a" * 64) is ClaimOutcome.UNAVAILABLE
    await cache.idempotency.mark_published(project, "k", "a" * 64)  # must not raise
    await cache.close()


async def test_api_key_cache_misses_and_reports_failed_invalidation() -> None:
    cache = build_redis_cache(make_settings(UNREACHABLE_DB, redis_url=UNREACHABLE_REDIS))
    entry = CachedApiKey(
        api_key_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="ci",
        key_prefix="abcdefghijkl",
        key_hash="f" * 64,
        scopes=("ingest:write",),
        expires_at=None,
    )

    await cache.api_keys.put(entry)  # must not raise
    assert await cache.api_keys.get(entry.key_prefix) is None
    assert await cache.api_keys.invalidate(entry.key_prefix) is False
    assert await cache.ping() is False
    await cache.close()
