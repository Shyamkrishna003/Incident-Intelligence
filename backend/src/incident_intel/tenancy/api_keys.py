"""API key format, generation, and hashing. Pure functions; no I/O.

Format: ``ii_<prefix>_<secret>``
  - prefix: 12 lowercase alphanumerics, a public lookup identifier (stored in clear)
  - secret: 43 URL-safe characters (256 bits of entropy)

Only ``HMAC-SHA256(pepper, full_key)`` is stored. A slow password hash (argon2/bcrypt) is
unnecessary because keys are high-entropy random values, not human-chosen passwords; the
server-side pepper means a database leak alone is not enough to verify guesses offline.
"""

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass
from enum import StrEnum

KEY_NAMESPACE = "ii"
PREFIX_LENGTH = 12
SECRET_BYTES = 32

_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
_KEY_PATTERN = re.compile(r"^ii_(?P<prefix>[a-z0-9]{12})_(?P<secret>[A-Za-z0-9_-]{43})$")


class ApiKeyScope(StrEnum):
    INGEST_WRITE = "ingest:write"
    TELEMETRY_READ = "telemetry:read"


DEFAULT_SCOPES = frozenset({ApiKeyScope.INGEST_WRITE, ApiKeyScope.TELEMETRY_READ})


@dataclass(frozen=True)
class GeneratedApiKey:
    plaintext: str
    prefix: str
    key_hash: str


def hash_api_key(plaintext: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), plaintext.encode(), hashlib.sha256).hexdigest()


def generate_api_key(pepper: str) -> GeneratedApiKey:
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))
    plaintext = f"{KEY_NAMESPACE}_{prefix}_{secrets.token_urlsafe(SECRET_BYTES)}"
    return GeneratedApiKey(
        plaintext=plaintext, prefix=prefix, key_hash=hash_api_key(plaintext, pepper)
    )


def parse_key_prefix(presented: str) -> str | None:
    """Return the lookup prefix if ``presented`` is well-formed, otherwise None."""
    match = _KEY_PATTERN.match(presented)
    return match.group("prefix") if match else None


def verify_api_key(presented: str, expected_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_api_key(presented, pepper), expected_hash)
