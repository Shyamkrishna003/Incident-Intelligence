"""Fixtures for cache tests: a controllable clock and an isolated real-Redis gateway."""

import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from incident_intel.cache.gateway import RedisGateway
from tests.conftest import _TestEnvironment


class Clock:
    """A clock tests move by hand. Starts exactly on a 60-second window boundary."""

    def __init__(self) -> None:
        self.now = 1_200_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def redis_client(test_environment: _TestEnvironment) -> AsyncIterator[Redis]:
    if test_environment.test_redis_url is None:
        pytest.skip("TEST_REDIS_URL is not set")
    client = Redis.from_url(
        test_environment.test_redis_url.get_secret_value(), decode_responses=True
    )
    yield client
    await client.aclose()


@pytest.fixture
async def redis_gateway(redis_client: Redis) -> AsyncIterator[RedisGateway]:
    """A gateway whose keys carry a random prefix; only those keys are deleted afterwards."""
    prefix = f"test-{uuid.uuid4().hex[:8]}:"
    yield RedisGateway(redis_client, key_prefix=prefix)
    async for key in redis_client.scan_iter(match=f"{prefix}*"):
        await redis_client.delete(key)
