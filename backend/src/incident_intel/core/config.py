"""Application configuration, loaded from environment variables (and `.env` in local dev).

Settings are layered so each process receives only what it needs:

- ``DatabaseSettings``: PostgreSQL access (migrations).
- ``RuntimeSettings``: + logging and Kafka (background consumers; no API secrets).
- ``Settings``: + API-only secrets and request limits (the HTTP API and admin CLI).
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_MIB = 1024 * 1024


class DatabaseSettings(BaseSettings):
    """The subset of settings needed to reach PostgreSQL (used by migrations too)."""

    # `.env` lives at the repository root; commands run from either the root or `backend/`.
    # Real environment variables always take precedence over both files.
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    database_url: SecretStr
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    db_pool_size: int = Field(default=5, ge=1)

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// driver")
        return value


class RuntimeSettings(DatabaseSettings):
    environment: Literal["local", "test", "production"] = "local"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    kafka_bootstrap_servers: str = "localhost:9094"
    kafka_client_id: str = "incident-intel"
    # Prepended to every topic name; lets tests or environments share one cluster.
    kafka_topic_prefix: str = Field(default="", pattern=r"^[A-Za-z0-9._-]*$")
    kafka_replication_factor: int = Field(default=1, ge=1)
    # Largest single Kafka message we produce or accept (a full ingestion batch).
    kafka_max_message_bytes: int = Field(default=2 * _MIB, ge=_MIB)


class Settings(RuntimeSettings):
    # Server-side secret mixed into API-key hashes. Rotating it invalidates all keys.
    api_key_pepper: SecretStr = Field(min_length=32)
    # Avoid a database write on every authenticated request.
    api_key_last_used_resolution_seconds: int = Field(default=60, ge=0)

    readiness_timeout_seconds: float = Field(default=2.0, gt=0)
    max_request_body_bytes: int = Field(default=_MIB, ge=1024)

    # How long the API waits for Kafka to acknowledge a batch before answering 503.
    kafka_produce_timeout_seconds: float = Field(default=5.0, gt=0)

    ingest_max_point_age_seconds: int = Field(default=7 * 24 * 3600, gt=0)
    ingest_max_future_skew_seconds: int = Field(default=300, ge=0)

    # Redis: a cache and limiter only, never the source of truth. SecretStr because the
    # URL may carry a password.
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    # Prepended to every key; lets tests or environments share one Redis.
    redis_key_prefix: str = Field(default="ii:", pattern=r"^[A-Za-z0-9._:-]*$")
    # Kept short: Redis is on the request path, and every use has a fallback.
    redis_timeout_seconds: float = Field(default=0.25, gt=0)

    # Each API key may make this many requests per window; beyond it the API answers 429.
    rate_limit_requests: int = Field(default=600, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    # While Redis is unreachable: True lets requests through, False rejects them (503).
    rate_limit_fail_open: bool = True

    # How long an Idempotency-Key is remembered at the API edge.
    idempotency_ttl_seconds: int = Field(default=24 * 3600, ge=60)
    # How long a verified API key is cached (0 disables the cache). Also the longest a
    # revoked key could keep working if Redis is unreachable at the moment of revocation.
    api_key_cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()
