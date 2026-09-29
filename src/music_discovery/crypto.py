"""Fernet encryption for Spotify tokens at rest.

The refresh token is the only long-lived secret stored in the database.
"""

from cryptography.fernet import Fernet, InvalidToken


def _fernet(key: str) -> Fernet:
    if not key:
        raise ValueError("TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise ValueError("TOKEN_ENCRYPTION_KEY must be a Fernet key") from exc


def encrypt_token(token: str, key: str) -> str:
    return _fernet(key).encrypt(token.encode()).decode()


def decrypt_token(ciphertext: str, key: str) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("could not decrypt token") from exc
