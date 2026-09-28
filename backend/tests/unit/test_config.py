import pytest
from pydantic import SecretStr, ValidationError

from incident_intel.core.config import Settings

VALID_URL = "postgresql+asyncpg://user:s3cret-password@localhost:5432/app"
VALID_PEPPER = "a" * 40


def test_accepts_valid_settings() -> None:
    settings = Settings(database_url=SecretStr(VALID_URL), api_key_pepper=SecretStr(VALID_PEPPER))

    assert settings.database_url.get_secret_value() == VALID_URL


def test_rejects_non_asyncpg_database_url() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        Settings(
            database_url=SecretStr("postgresql://user:pw@localhost/app"),
            api_key_pepper=SecretStr(VALID_PEPPER),
        )


def test_rejects_short_pepper() -> None:
    with pytest.raises(ValidationError, match="api_key_pepper"):
        Settings(database_url=SecretStr(VALID_URL), api_key_pepper=SecretStr("short"))


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = Settings(database_url=SecretStr(VALID_URL), api_key_pepper=SecretStr(VALID_PEPPER))

    rendered = repr(settings) + str(settings.model_dump())
    assert "s3cret-password" not in rendered
    assert VALID_PEPPER not in rendered
