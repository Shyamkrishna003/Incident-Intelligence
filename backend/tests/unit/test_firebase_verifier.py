"""The real Firebase verifier, exercised without network access.

Tokens that are structurally wrong are rejected before any signing key is fetched. The
emulator path (unsigned tokens accepted) is also covered offline, because accepting an
unsigned token anywhere but local development would be a serious hole.
"""

import base64
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from incident_intel.auth.tokens import (
    EMULATOR_ENV,
    DisabledTokenVerifier,
    FirebaseTokenVerifier,
    build_token_verifier,
    identity_from_claims,
)
from incident_intel.core.errors import AuthenticationError, ServiceUnavailableError
from tests.support import make_settings

PROJECT = "demo-incident-intel"
DB = "postgresql+asyncpg://user:pw@127.0.0.1:1/unreachable_test"


def _segment(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def unsigned_token(**overrides: Any) -> str:
    """A JWT with valid Firebase claims but no signature (what the emulator issues)."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "user-123",
        "user_id": "user-123",
        "iat": now - 10,
        "auth_time": now - 10,
        "exp": now + 3600,
        "email": "priya@example.test",
        "email_verified": True,
        "name": "Priya",
        "firebase": {"sign_in_provider": "google.com"},
        **overrides,
    }
    return f"{_segment({'alg': 'none', 'typ': 'JWT'})}.{_segment(claims)}."


@pytest.fixture
async def verifier(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FirebaseTokenVerifier]:
    monkeypatch.delenv(EMULATOR_ENV, raising=False)
    instance = FirebaseTokenVerifier(project_id=PROJECT)
    yield instance
    await instance.close()


@pytest.fixture
async def emulator_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FirebaseTokenVerifier]:
    # setenv first so monkeypatch restores the environment after the test.
    monkeypatch.setenv(EMULATOR_ENV, "127.0.0.1:9099")
    instance = FirebaseTokenVerifier(project_id=PROJECT, emulator_host="127.0.0.1:9099")
    yield instance
    await instance.close()


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        ("", "invalid_token"),
        ("not-a-jwt", "invalid_token"),
        ("a.b.c", "invalid_token"),
        ("ii_abcdefghijkl_" + "a" * 43, "not_an_id_token"),  # an API key on a user endpoint
        ("x" * 9000, "not_an_id_token"),
    ],
    ids=["empty", "garbage", "three-parts", "api-key", "oversized"],
)
async def test_rejects_malformed_tokens(
    verifier: FirebaseTokenVerifier, token: str, reason: str
) -> None:
    with pytest.raises(AuthenticationError) as excinfo:
        await verifier.verify(token)

    assert excinfo.value.reason == reason


async def test_rejects_unsigned_tokens_outside_emulator_mode(
    verifier: FirebaseTokenVerifier,
) -> None:
    with pytest.raises(AuthenticationError):
        await verifier.verify(unsigned_token())


async def test_emulator_mode_accepts_unsigned_tokens_and_maps_claims(
    emulator_verifier: FirebaseTokenVerifier,
) -> None:
    identity = await emulator_verifier.verify(unsigned_token())

    assert identity.uid == "user-123"
    assert identity.email == "priya@example.test"
    assert identity.email_verified is True
    assert identity.display_name == "Priya"
    assert identity.sign_in_provider == "google.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "some-other-project"},
        {"iss": "https://securetoken.google.com/some-other-project"},
        {"sub": ""},
    ],
    ids=["wrong-audience", "wrong-issuer", "no-subject"],
)
async def test_emulator_mode_still_checks_the_claims(
    emulator_verifier: FirebaseTokenVerifier, overrides: dict[str, Any]
) -> None:
    with pytest.raises(AuthenticationError):
        await emulator_verifier.verify(unsigned_token(**overrides))


def test_identity_ignores_claims_of_the_wrong_type() -> None:
    identity = identity_from_claims(
        {"uid": "u1", "email": 42, "email_verified": "true", "name": None, "firebase": "x"}
    )

    assert identity.uid == "u1"
    assert identity.email is None
    assert identity.email_verified is False  # only a real boolean true counts
    assert identity.display_name is None
    assert identity.sign_in_provider is None


async def test_sign_in_is_unavailable_without_a_project_id() -> None:
    verifier = build_token_verifier(make_settings(DB))

    assert isinstance(verifier, DisabledTokenVerifier)
    with pytest.raises(ServiceUnavailableError):
        await verifier.verify("anything")


def test_emulator_is_refused_in_production() -> None:
    with pytest.raises(ValidationError, match="FIREBASE_AUTH_EMULATOR_HOST"):
        make_settings(
            DB,
            environment="production",
            firebase_project_id=PROJECT,
            firebase_auth_emulator_host="localhost:9099",
        )


def test_emulator_is_allowed_locally() -> None:
    settings = make_settings(
        DB, environment="local", firebase_project_id=PROJECT, firebase_auth_emulator_host="x:1"
    )

    assert settings.firebase_auth_emulator_host == "x:1"
