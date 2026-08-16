"""Envelope encryption for credentials at rest.

Everything sensitive (SSH private keys, bootstrap passwords, harness API keys)
is stored as Fernet ciphertext in the `private` schema. The key comes from the
environment, never the database, so a Postgres dump on its own is inert.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class DecryptionError(RuntimeError):
    """Ciphertext could not be decrypted with the configured key."""


def _fernet() -> Fernet:
    settings = get_settings()
    try:
        return Fernet(settings.moonphase_secret_key.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "MOONPHASE_SECRET_KEY is not a valid Fernet key (32 url-safe base64 bytes)."
        ) from exc


def encrypt(plaintext: str | None) -> bytes | None:
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode())


def decrypt(ciphertext: bytes | memoryview | None) -> str | None:
    if ciphertext is None:
        return None
    if isinstance(ciphertext, memoryview):
        ciphertext = bytes(ciphertext)
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Stored credential could not be decrypted. MOONPHASE_SECRET_KEY has "
            "probably changed since it was written."
        ) from exc
