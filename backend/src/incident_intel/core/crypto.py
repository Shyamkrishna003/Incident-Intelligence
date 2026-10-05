"""Encrypting secrets that have to be stored, such as a project's GitHub token.

Fernet (AES-128-CBC with an HMAC) from the ``cryptography`` package: the stored value is
unreadable and tamper-evident without the key, which lives only in the environment
(``SECRETS_ENCRYPTION_KEY``). A database leak alone does not reveal the secrets.
"""

from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptionError(Exception):
    """A stored secret could not be decrypted (wrong key, or the value was altered)."""


def validate_encryption_key(key: str) -> str:
    """Return ``key`` if it is a usable Fernet key; raise ValueError otherwise."""
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        # The message must never include the key itself.
        raise ValueError(
            "must be a Fernet key (32 random bytes, URL-safe base64); generate one with "
            "`make secrets-key`"
        ) from exc
    return key


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(validate_encryption_key(key).encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, ValueError) as exc:
            raise SecretDecryptionError("stored secret could not be decrypted") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()
