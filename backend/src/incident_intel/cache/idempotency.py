"""Idempotency-key claims at the API edge.

For each (project, Idempotency-Key) the store remembers the content hash of the batch and
whether Kafka has confirmed it:

    <content_sha256>:pending     claimed; publish not yet confirmed
    <content_sha256>:published   Kafka acknowledged the batch

This lets the API answer two cases immediately instead of leaving them to the storage
consumer: the same key with *different* data (409), and a retry of a batch that was already
published (return the same batch id without publishing again).

A retry of a ``pending`` claim publishes again on purpose: the first attempt may have
crashed before reaching Kafka, and the storage consumer drops real duplicates anyway. The
store is an optimization and an early warning, never the durability guarantee.
"""

import uuid
from enum import StrEnum
from typing import Protocol

from redis.asyncio import Redis

from incident_intel.cache.gateway import CacheUnavailableError, RedisGateway, as_text

_PENDING = "pending"
_PUBLISHED = "published"


class ClaimOutcome(StrEnum):
    NEW = "new"  # first time this key is seen
    IN_FLIGHT = "in_flight"  # seen before with the same content; publish unconfirmed
    PUBLISHED = "published"  # seen before with the same content; already in Kafka
    CONFLICT = "conflict"  # seen before with different content
    UNAVAILABLE = "unavailable"  # the store could not be reached


class IdempotencyStore(Protocol):
    async def claim(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> ClaimOutcome: ...

    async def mark_published(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> None: ...


def classify_existing(stored: str | None, content_sha256: str) -> ClaimOutcome:
    """Interpret the value already stored for a key."""
    if stored is None:  # expired between our SET NX and GET: publishing again is safe
        return ClaimOutcome.IN_FLIGHT
    stored_hash, _, state = stored.rpartition(":")
    if stored_hash != content_sha256:
        return ClaimOutcome.CONFLICT
    return ClaimOutcome.PUBLISHED if state == _PUBLISHED else ClaimOutcome.IN_FLIGHT


class RedisIdempotencyStore:
    def __init__(self, gateway: RedisGateway, *, ttl_seconds: int) -> None:
        self._gateway = gateway
        self._ttl = ttl_seconds

    def _key(self, project_id: uuid.UUID, idempotency_key: str) -> str:
        return self._gateway.key("idem", project_id, idempotency_key)

    async def claim(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> ClaimOutcome:
        key = self._key(project_id, idempotency_key)

        async def claim_or_read(client: Redis) -> ClaimOutcome:
            # SET NX is atomic: of two concurrent first requests, exactly one gets NEW.
            created = await client.set(key, f"{content_sha256}:{_PENDING}", nx=True, ex=self._ttl)
            if created:
                return ClaimOutcome.NEW
            return classify_existing(as_text(await client.get(key)), content_sha256)

        try:
            return await self._gateway.run(claim_or_read)
        except CacheUnavailableError:
            return ClaimOutcome.UNAVAILABLE

    async def mark_published(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> None:
        key = self._key(project_id, idempotency_key)

        async def mark(client: Redis) -> None:
            # XX: only if the claim still exists. KEEPTTL: do not extend its lifetime.
            await client.set(key, f"{content_sha256}:{_PUBLISHED}", xx=True, keepttl=True)

        try:
            await self._gateway.run(mark)
        except CacheUnavailableError:
            return  # the claim stays pending: a retry publishes again, which is safe
