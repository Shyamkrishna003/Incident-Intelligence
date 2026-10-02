"""Behavior every cache backend must share.

Each test runs twice: against the in-memory ``FakeCache`` used by the rest of the suite and
against the real Redis implementations. If the fake ever behaves differently from Redis,
this file fails, so tests built on the fake stay trustworthy.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from incident_intel.cache.api_keys import ApiKeyCache, CachedApiKey, RedisApiKeyCache
from incident_intel.cache.idempotency import ClaimOutcome, IdempotencyStore, RedisIdempotencyStore
from incident_intel.cache.rate_limit import RateLimiter, RedisRateLimiter
from tests.cache.conftest import Clock
from tests.support import FakeCache

LIMIT = 3
WINDOW = 60
HASH_A = "a" * 64
HASH_B = "b" * 64


@dataclass(frozen=True)
class Backend:
    rate_limiter: RateLimiter
    idempotency: IdempotencyStore
    api_keys: ApiKeyCache


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.redis)])
def backend(request: pytest.FixtureRequest, clock: Clock) -> Backend:
    if request.param == "memory":
        fake = FakeCache(limit=LIMIT, window_seconds=WINDOW, clock=clock)
        return Backend(fake, fake, fake)
    gateway = request.getfixturevalue("redis_gateway")
    return Backend(
        RedisRateLimiter(gateway, limit=LIMIT, window_seconds=WINDOW, fail_open=True, clock=clock),
        RedisIdempotencyStore(gateway, ttl_seconds=3600),
        RedisApiKeyCache(gateway, ttl_seconds=60),
    )


def _entry(prefix: str = "abcdefghijkl") -> CachedApiKey:
    return CachedApiKey(
        api_key_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="ci",
        key_prefix=prefix,
        key_hash="f" * 64,
        scopes=("ingest:write", "telemetry:read"),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


# --- Rate limiter ---------------------------------------------------------------------


async def test_allows_up_to_the_limit_then_rejects(backend: Backend) -> None:
    decisions = [await backend.rate_limiter.check("key-1") for _ in range(LIMIT + 2)]

    assert [d.allowed for d in decisions] == [True] * LIMIT + [False, False]
    assert all(not d.limiter_unavailable for d in decisions)


async def test_rejection_says_how_long_until_the_window_ends(
    backend: Backend, clock: Clock
) -> None:
    clock.now += 45
    for _ in range(LIMIT):
        await backend.rate_limiter.check("key-1")

    rejected = await backend.rate_limiter.check("key-1")

    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 15


async def test_subjects_are_limited_independently(backend: Backend) -> None:
    for _ in range(LIMIT + 1):
        await backend.rate_limiter.check("noisy")

    assert (await backend.rate_limiter.check("quiet")).allowed is True


async def test_budget_returns_in_the_next_window(backend: Backend, clock: Clock) -> None:
    for _ in range(LIMIT + 1):
        await backend.rate_limiter.check("key-1")
    assert (await backend.rate_limiter.check("key-1")).allowed is False

    clock.now += WINDOW

    assert (await backend.rate_limiter.check("key-1")).allowed is True


# --- Idempotency store ----------------------------------------------------------------


async def test_first_claim_is_new_and_a_repeat_is_in_flight(backend: Backend) -> None:
    project = uuid.uuid4()

    assert await backend.idempotency.claim(project, "k", HASH_A) is ClaimOutcome.NEW
    assert await backend.idempotency.claim(project, "k", HASH_A) is ClaimOutcome.IN_FLIGHT


async def test_repeat_after_publish_is_published(backend: Backend) -> None:
    project = uuid.uuid4()
    await backend.idempotency.claim(project, "k", HASH_A)

    await backend.idempotency.mark_published(project, "k", HASH_A)

    assert await backend.idempotency.claim(project, "k", HASH_A) is ClaimOutcome.PUBLISHED


@pytest.mark.parametrize("published", [False, True], ids=["pending", "published"])
async def test_same_key_with_different_content_conflicts(backend: Backend, published: bool) -> None:
    project = uuid.uuid4()
    await backend.idempotency.claim(project, "k", HASH_A)
    if published:
        await backend.idempotency.mark_published(project, "k", HASH_A)

    assert await backend.idempotency.claim(project, "k", HASH_B) is ClaimOutcome.CONFLICT
    # The original claim is untouched by the conflicting attempt.
    expected = ClaimOutcome.PUBLISHED if published else ClaimOutcome.IN_FLIGHT
    assert await backend.idempotency.claim(project, "k", HASH_A) is expected


async def test_same_key_in_another_project_is_unrelated(backend: Backend) -> None:
    await backend.idempotency.claim(uuid.uuid4(), "k", HASH_A)

    assert await backend.idempotency.claim(uuid.uuid4(), "k", HASH_B) is ClaimOutcome.NEW


async def test_mark_published_does_not_create_a_claim(backend: Backend) -> None:
    project = uuid.uuid4()

    await backend.idempotency.mark_published(project, "never-claimed", HASH_A)

    assert await backend.idempotency.claim(project, "never-claimed", HASH_A) is ClaimOutcome.NEW


# --- API-key cache --------------------------------------------------------------------


async def test_miss_returns_none(backend: Backend) -> None:
    assert await backend.api_keys.get("zzzzzzzzzzzz") is None


async def test_put_then_get_round_trips_the_entry(backend: Backend) -> None:
    entry = _entry()

    await backend.api_keys.put(entry)

    assert await backend.api_keys.get(entry.key_prefix) == entry


async def test_invalidate_removes_the_entry(backend: Backend) -> None:
    entry = _entry()
    await backend.api_keys.put(entry)

    assert await backend.api_keys.invalidate(entry.key_prefix) is True
    assert await backend.api_keys.get(entry.key_prefix) is None


async def test_invalidating_a_missing_entry_succeeds(backend: Backend) -> None:
    assert await backend.api_keys.invalidate("zzzzzzzzzzzz") is True
