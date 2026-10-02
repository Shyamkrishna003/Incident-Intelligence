"""Helpers shared by unit and integration tests."""

import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import SecretStr

from incident_intel.auth.tokens import VerifiedIdentity
from incident_intel.cache.api_keys import CachedApiKey
from incident_intel.cache.idempotency import ClaimOutcome, classify_existing
from incident_intel.cache.rate_limit import RateLimitDecision, window_position
from incident_intel.cache.services import CacheServices
from incident_intel.core.config import Settings
from incident_intel.core.errors import AuthenticationError, ServiceUnavailableError
from incident_intel.investigation.llm import LLMError, LLMResponse
from incident_intel.streaming.producer import PublishError
from incident_intel.telemetry.messages import (
    MetricBatchMessage,
    MetricPointMessage,
    points_content_hash,
)

TEST_PEPPER = "test-pepper-" + "x" * 40


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr(database_url),
        "api_key_pepper": SecretStr(TEST_PEPPER),
        "environment": "test",
        # Explicit, so tests never pick these up from a developer's .env.
        "firebase_project_id": None,
        "firebase_auth_emulator_host": None,
        "log_json": True,
        **overrides,
    }
    return Settings(**values)


@dataclass(frozen=True)
class PublishedMessage:
    topic: str
    key: bytes | None
    value: bytes
    headers: dict[str, str]


@dataclass
class FakePublisher:
    """In-memory stand-in for Kafka. Records messages; can simulate outages."""

    fail_publish: bool = False
    unreachable: bool = False
    missing: set[str] = field(default_factory=set)
    messages: list[PublishedMessage] = field(default_factory=list)
    closed: bool = False

    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if self.fail_publish:
            raise PublishError("simulated broker outage")
        self.messages.append(PublishedMessage(topic, key, value, dict(headers or {})))

    async def missing_topics(self, topics: Iterable[str], *, timeout_seconds: float) -> set[str]:
        if self.unreachable:
            raise PublishError("simulated broker outage")
        return set(topics) & self.missing

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeCache:
    """In-memory stand-in for the Redis-backed services. Can simulate a Redis outage.

    `tests/cache/test_cache_contract.py` runs the same behavioral tests against this class
    and the real Redis implementations, so the two cannot drift apart unnoticed.
    """

    limit: int = 1000
    window_seconds: int = 60
    fail_open: bool = True
    unavailable: bool = False
    clock: Callable[[], float] = time.time
    counters: dict[tuple[str, int], int] = field(default_factory=dict)
    claims: dict[tuple[uuid.UUID, str], str] = field(default_factory=dict)
    api_keys: dict[str, CachedApiKey] = field(default_factory=dict)
    closed: bool = False

    # --- RateLimiter ---
    async def check(self, subject: str) -> RateLimitDecision:
        if self.unavailable:
            return RateLimitDecision(allowed=self.fail_open, limiter_unavailable=True)
        index, seconds_left = window_position(self.clock(), self.window_seconds)
        count = self.counters.get((subject, index), 0) + 1
        self.counters[subject, index] = count
        if count > self.limit:
            return RateLimitDecision(allowed=False, retry_after_seconds=seconds_left)
        return RateLimitDecision(allowed=True)

    # --- IdempotencyStore ---
    async def claim(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> ClaimOutcome:
        if self.unavailable:
            return ClaimOutcome.UNAVAILABLE
        key = (project_id, idempotency_key)
        if key not in self.claims:
            self.claims[key] = f"{content_sha256}:pending"
            return ClaimOutcome.NEW
        return classify_existing(self.claims[key], content_sha256)

    async def mark_published(
        self, project_id: uuid.UUID, idempotency_key: str, content_sha256: str
    ) -> None:
        key = (project_id, idempotency_key)
        if not self.unavailable and key in self.claims:
            self.claims[key] = f"{content_sha256}:published"

    # --- ApiKeyCache ---
    async def get(self, key_prefix: str) -> CachedApiKey | None:
        return None if self.unavailable else self.api_keys.get(key_prefix)

    async def put(self, entry: CachedApiKey) -> None:
        if not self.unavailable:
            self.api_keys[entry.key_prefix] = entry

    async def invalidate(self, key_prefix: str) -> bool:
        if self.unavailable:
            return False
        self.api_keys.pop(key_prefix, None)
        return True

    # --- lifecycle ---
    async def ping(self) -> bool:
        return not self.unavailable

    async def close(self) -> None:
        self.closed = True

    def services(self) -> CacheServices:
        return CacheServices(
            rate_limiter=self, idempotency=self, api_keys=self, ping=self.ping, close=self.close
        )


@dataclass
class FakeTokenVerifier:
    """Stand-in for Firebase: `issue()` hands out a token for an identity; only issued
    tokens verify. Can simulate the verification service being down."""

    identities: dict[str, VerifiedIdentity] = field(default_factory=dict)
    unavailable: bool = False

    def issue(
        self,
        *,
        uid: str | None = None,
        email: str | None = None,
        email_verified: bool = True,
        display_name: str | None = None,
        sign_in_provider: str = "password",
    ) -> str:
        uid = uid or f"uid-{uuid.uuid4().hex[:12]}"
        token = f"test-token-{uuid.uuid4().hex}"
        self.identities[token] = VerifiedIdentity(
            uid=uid,
            email=email if email is not None else f"{uid}@example.test",
            email_verified=email_verified,
            display_name=display_name,
            sign_in_provider=sign_in_provider,
        )
        return token

    async def verify(self, token: str) -> VerifiedIdentity:
        if self.unavailable:
            raise ServiceUnavailableError("Sign-in verification is temporarily unavailable.")
        identity = self.identities.get(token)
        if identity is None:
            raise AuthenticationError("invalid_token")
        return identity

    async def close(self) -> None:
        return None


@dataclass
class FakeLLM:
    """A scripted model: returns the given replies in order and records what it was sent.
    A reply can be text, or an LLMError to raise."""

    replies: list[str | LLMError] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-model"

    async def generate_json(self, *, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        if not self.replies:
            raise AssertionError("the model was called more often than the test scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, LLMError):
            raise reply
        return LLMResponse(text=reply, input_tokens=100, output_tokens=20)

    async def close(self) -> None:
        return None


def metric_point(
    timestamp: datetime,
    value: float,
    *,
    service: str = "payment-api",
    metric: str = "http.server.duration.p95",
    unit: str | None = "ms",
    attributes: dict[str, str] | None = None,
) -> MetricPointMessage:
    return MetricPointMessage(
        service=service,
        metric=metric,
        unit=unit,
        timestamp=timestamp,
        value=value,
        attributes=attributes or {},
    )


def metric_batch(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    api_key_id: uuid.UUID,
    points: list[MetricPointMessage],
    received_at: datetime,
    batch_id: uuid.UUID | None = None,
    idempotency_key: str = "test-key",
) -> MetricBatchMessage:
    return MetricBatchMessage(
        batch_id=batch_id or uuid.uuid4(),
        organization_id=organization_id,
        project_id=project_id,
        api_key_id=api_key_id,
        idempotency_key=idempotency_key,
        content_sha256=points_content_hash(points),
        received_at=received_at,
        points=points,
    )
