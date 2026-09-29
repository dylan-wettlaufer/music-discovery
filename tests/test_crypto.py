import pytest
from cryptography.fernet import Fernet

from music_discovery.crypto import decrypt_token, encrypt_token


def test_token_round_trip():
    key = Fernet.generate_key().decode()
    ciphertext = encrypt_token("refresh-token", key)

    assert ciphertext != "refresh-token"
    assert decrypt_token(ciphertext, key) == "refresh-token"


def test_missing_key_is_rejected():
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        encrypt_token("refresh-token", "")


def test_wrong_key_is_rejected():
    key = Fernet.generate_key().decode()
    other = Fernet.generate_key().decode()
    ciphertext = encrypt_token("refresh-token", key)

    with pytest.raises(ValueError, match="could not decrypt"):
        decrypt_token(ciphertext, other)
