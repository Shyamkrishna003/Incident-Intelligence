"""Helpers shared by unit and integration tests."""

from typing import Any

from pydantic import SecretStr

from incident_intel.core.config import Settings

TEST_PEPPER = "test-pepper-" + "x" * 40


def make_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": SecretStr(database_url),
        "api_key_pepper": SecretStr(TEST_PEPPER),
        "environment": "test",
        "log_json": True,
        **overrides,
    }
    return Settings(**values)
