"""Application configuration, loaded from environment variables (and `.env` in local dev)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


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


class Settings(DatabaseSettings):
    environment: Literal["local", "test", "production"] = "local"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # Server-side secret mixed into API-key hashes. Rotating it invalidates all keys.
    api_key_pepper: SecretStr = Field(min_length=32)
    # Avoid a database write on every authenticated request.
    api_key_last_used_resolution_seconds: int = Field(default=60, ge=0)

    readiness_timeout_seconds: float = Field(default=2.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
