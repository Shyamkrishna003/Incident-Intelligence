"""Short-lived cache of verified API-key records, keyed by the key's public prefix.

A cached entry holds what authentication needs without a database query: the key's HMAC
(never the plaintext key), its tenant ids, scopes, and expiry. Entries live for a short TTL
and are deleted on revocation, so a revoked key stops working at once when Redis is
reachable, and after at most one TTL otherwise.
"""

import uuid
from typing import Protocol

import structlog
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

from incident_intel.cache.gateway import CacheUnavailableError, RedisGateway, as_text

logger = structlog.get_logger(__name__)


class CachedApiKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    api_key_id: uuid.UUID
    organization_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    key_prefix: str
    key_hash: str
    scopes: tuple[str, ...]
    expires_at: AwareDatetime | None


class ApiKeyCache(Protocol):
    async def get(self, key_prefix: str) -> CachedApiKey | None: ...

    async def put(self, entry: CachedApiKey) -> None: ...

    async def invalidate(self, key_prefix: str) -> bool:
        """Remove an entry. Returns False if the cache could not be reached."""
        ...


class RedisApiKeyCache:
    def __init__(self, gateway: RedisGateway, *, ttl_seconds: int) -> None:
        self._gateway = gateway
        self._ttl = ttl_seconds

    def _key(self, key_prefix: str) -> str:
        return self._gateway.key("apikey", key_prefix)

    async def get(self, key_prefix: str) -> CachedApiKey | None:
        if self._ttl == 0:
            return None
        key = self._key(key_prefix)

        async def read(client: Redis) -> str | None:
            return as_text(await client.get(key))

        try:
            raw = await self._gateway.run(read)
        except CacheUnavailableError:
            return None  # fall back to the database
        if raw is None:
            return None
        try:
            return CachedApiKey.model_validate_json(raw)
        except ValidationError:
            # An entry written by an older version of this model: treat as a miss.
            logger.warning("api_key_cache_entry_invalid")
            return None

    async def put(self, entry: CachedApiKey) -> None:
        if self._ttl == 0:
            return
        key = self._key(entry.key_prefix)
        value = entry.model_dump_json()

        async def write(client: Redis) -> None:
            await client.set(key, value, ex=self._ttl)

        try:
            await self._gateway.run(write)
        except CacheUnavailableError:
            return

    async def invalidate(self, key_prefix: str) -> bool:
        key = self._key(key_prefix)

        async def delete(client: Redis) -> None:
            await client.delete(key)

        try:
            await self._gateway.run(delete)
        except CacheUnavailableError:
            return False
        return True
