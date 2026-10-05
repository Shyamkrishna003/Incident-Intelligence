"""Encryption of stored secrets."""

import pytest
from pydantic import SecretStr, ValidationError

from incident_intel.core.crypto import (
    SecretBox,
    SecretDecryptionError,
    generate_key,
    validate_encryption_key,
)
from tests.support import make_settings

URL = "postgresql+asyncpg://u:p@localhost/db"


def test_round_trip_and_ciphertext_does_not_contain_the_secret() -> None:
    box = SecretBox(generate_key())

    stored = box.encrypt("github_pat_secretvalue")

    assert "secretvalue" not in stored
    assert box.decrypt(stored) == "github_pat_secretvalue"


def test_the_same_secret_encrypts_differently_each_time() -> None:
    box = SecretBox(generate_key())
    assert box.encrypt("same") != box.encrypt("same")


def test_another_key_cannot_decrypt() -> None:
    stored = SecretBox(generate_key()).encrypt("secret")
    with pytest.raises(SecretDecryptionError):
        SecretBox(generate_key()).decrypt(stored)


def test_an_altered_value_is_rejected() -> None:
    box = SecretBox(generate_key())
    stored = box.encrypt("secret")
    altered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretDecryptionError):
        box.decrypt(altered)
    with pytest.raises(SecretDecryptionError):
        box.decrypt("not a fernet token")


def test_an_unusable_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="Fernet key") as caught:
        validate_encryption_key("my-weak-key")
    assert "my-weak-key" not in str(caught.value)


def test_settings_accept_a_valid_key_and_treat_empty_as_unset() -> None:
    key = generate_key()
    configured = make_settings(URL, secrets_encryption_key=SecretStr(key)).secrets_encryption_key
    assert configured is not None
    assert configured.get_secret_value() == key
    assert make_settings(URL, secrets_encryption_key="").secrets_encryption_key is None


def test_settings_refuse_an_unusable_key_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(URL, secrets_encryption_key=SecretStr("my-weak-key"))
    assert "my-weak-key" not in str(caught.value)
