"""Against the real Firebase Auth emulator: a token it issues is accepted by our verifier
and by the API. Needs `make emulator` and TEST_FIREBASE_AUTH_EMULATOR_HOST."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.auth.tokens import EMULATOR_ENV, FirebaseTokenVerifier
from incident_intel.core.config import Settings
from incident_intel.db.session import get_session
from incident_intel.main import create_app
from tests.conftest import _TestEnvironment
from tests.support import FakeCache, FakePublisher

pytestmark = [pytest.mark.integration, pytest.mark.firebase]

# Must match the project the emulator was started with (infra/firebase-emulator).
PROJECT = "demo-incident-intel"


@pytest.fixture
def emulator_host(test_environment: _TestEnvironment, monkeypatch: pytest.MonkeyPatch) -> str:
    host = test_environment.test_firebase_auth_emulator_host
    if not host:
        pytest.skip("TEST_FIREBASE_AUTH_EMULATOR_HOST is not set")
    # Set through monkeypatch so the process environment is restored after the test.
    monkeypatch.setenv(EMULATOR_ENV, host)
    return host


@pytest.fixture
async def verifier(emulator_host: str) -> AsyncIterator[FirebaseTokenVerifier]:
    instance = FirebaseTokenVerifier(project_id=PROJECT, emulator_host=emulator_host)
    yield instance
    await instance.close()


async def _sign_up(emulator_host: str, email: str) -> str:
    """Create an email/password account in the emulator and return its ID token."""
    async with httpx.AsyncClient() as http:
        response = await http.post(
            f"http://{emulator_host}/identitytoolkit.googleapis.com/v1/accounts:signUp",
            params={"key": "fake-api-key"},
            json={"email": email, "password": "correct horse battery", "returnSecureToken": True},
        )
    response.raise_for_status()
    token: str = response.json()["idToken"]
    return token


async def test_emulator_token_is_verified(
    emulator_host: str, verifier: FirebaseTokenVerifier
) -> None:
    email = f"user-{uuid.uuid4().hex[:8]}@example.test"
    token = await _sign_up(emulator_host, email)

    identity = await verifier.verify(token)

    assert identity.email == email
    assert identity.email_verified is False  # a fresh email/password account
    assert identity.sign_in_provider == "password"
    assert identity.uid


async def test_emulator_user_can_call_the_api(
    emulator_host: str,
    verifier: FirebaseTokenVerifier,
    settings: Settings,
    db_session: AsyncSession,
) -> None:
    app: FastAPI = create_app(
        settings,
        publisher=FakePublisher(),
        cache=FakeCache().services(),
        token_verifier=verifier,
    )

    async def _test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _test_session
    email = f"user-{uuid.uuid4().hex[:8]}@example.test"
    token = await _sign_up(emulator_host, email)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        me = await http.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        # An unverified email may sign in but not create an organization.
        create = await http.post(
            "/v1/organizations",
            json={"slug": f"org-{uuid.uuid4().hex[:8]}", "name": "Acme"},
            headers={"Authorization": f"Bearer {token}"},
        )
    await app.state.engine.dispose()

    assert me.status_code == 200
    assert me.json()["user"]["email"] == email
    assert create.status_code == 403
