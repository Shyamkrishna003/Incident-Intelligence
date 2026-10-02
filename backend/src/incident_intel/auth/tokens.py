"""Verifying sign-in tokens for human users.

Firebase Authentication proves *who* a person is: the browser signs in with Firebase and
sends the resulting ID token (a signed JWT) to this API. This module checks that token.
What the person may do is decided separately, from memberships in our own database.

Verification needs only the Firebase project ID (the token's audience); Google's public
signing keys are fetched and cached by the SDK. No service-account secret is involved.
"""

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import firebase_admin
import structlog
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials as firebase_credentials
from firebase_admin import exceptions as firebase_exceptions
from google.auth.credentials import AnonymousCredentials

from incident_intel.core.config import Settings
from incident_intel.core.errors import AuthenticationError, ServiceUnavailableError

logger = structlog.get_logger(__name__)

# The Firebase SDK switches to emulator mode when this is set: unsigned tokens are accepted
# and token expiry is not checked. Local development only.
EMULATOR_ENV = "FIREBASE_AUTH_EMULATOR_HOST"
# Real ID tokens are roughly 1-2 kB; anything far larger is not worth parsing.
_MAX_TOKEN_LENGTH = 8192


@dataclass(frozen=True)
class VerifiedIdentity:
    """Claims taken from a verified ID token."""

    uid: str
    email: str | None
    email_verified: bool
    display_name: str | None
    sign_in_provider: str | None


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> VerifiedIdentity:
        """Return the token's identity, or raise AuthenticationError."""
        ...

    async def close(self) -> None: ...


def identity_from_claims(claims: dict[str, Any]) -> VerifiedIdentity:
    firebase_claims = claims.get("firebase")
    provider = (
        firebase_claims.get("sign_in_provider") if isinstance(firebase_claims, dict) else None
    )
    email = claims.get("email")
    name = claims.get("name")
    return VerifiedIdentity(
        uid=str(claims["uid"]),
        email=email if isinstance(email, str) else None,
        email_verified=claims.get("email_verified") is True,
        display_name=name if isinstance(name, str) else None,
        sign_in_provider=provider if isinstance(provider, str) else None,
    )


class _NoCredential(firebase_credentials.Base):  # type: ignore[misc]  # untyped base class
    """Tells the SDK to act without Google credentials.

    Checking an ID token uses only Google's public signing keys. Without this, the SDK
    looks for "application default credentials" and fails (slowly) when none exist.
    """

    def get_credential(self) -> AnonymousCredentials:
        return AnonymousCredentials()  # type: ignore[no-untyped-call]


class FirebaseTokenVerifier:
    def __init__(
        self, *, project_id: str, emulator_host: str | None = None, clock_skew_seconds: int = 10
    ) -> None:
        if emulator_host:
            # Read by the SDK from the process environment at verification time.
            os.environ[EMULATOR_ENV] = emulator_host
            logger.warning("firebase_auth_emulator_enabled", emulator_host=emulator_host)
        self._clock_skew_seconds = clock_skew_seconds
        # A uniquely named app: several verifiers (tests) can coexist in one process.
        self._app = firebase_admin.initialize_app(
            credential=_NoCredential(),
            options={"projectId": project_id},
            name=f"incident-intel-{uuid.uuid4().hex}",
        )

    async def verify(self, token: str) -> VerifiedIdentity:
        if len(token) > _MAX_TOKEN_LENGTH or token.startswith("ii_"):
            # An oversized value, or a project API key sent to a user endpoint.
            raise AuthenticationError("not_an_id_token")
        try:
            # Signature checking is CPU work plus an occasional key fetch: keep it off
            # the event loop.
            claims = await asyncio.to_thread(
                firebase_auth.verify_id_token,
                token,
                app=self._app,
                clock_skew_seconds=self._clock_skew_seconds,
            )
        except firebase_auth.ExpiredIdTokenError as exc:
            raise AuthenticationError("expired_token") from exc
        except (firebase_auth.InvalidIdTokenError, ValueError) as exc:
            raise AuthenticationError("invalid_token") from exc
        except firebase_exceptions.FirebaseError as exc:
            # For example Google's signing keys could not be fetched.
            logger.warning("id_token_verification_unavailable", error_type=type(exc).__name__)
            raise ServiceUnavailableError(
                "Sign-in verification is temporarily unavailable."
            ) from exc
        return identity_from_claims(claims)

    async def close(self) -> None:
        firebase_admin.delete_app(self._app)


class DisabledTokenVerifier:
    """Used when FIREBASE_PROJECT_ID is not configured: user sign-in is unavailable."""

    async def verify(self, token: str) -> VerifiedIdentity:
        raise ServiceUnavailableError("User sign-in is not configured on this server.")

    async def close(self) -> None:
        return None


def build_token_verifier(settings: Settings) -> TokenVerifier:
    if settings.firebase_project_id is None:
        logger.warning("user_sign_in_disabled", reason="FIREBASE_PROJECT_ID is not set")
        return DisabledTokenVerifier()
    return FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        emulator_host=settings.firebase_auth_emulator_host,
    )
