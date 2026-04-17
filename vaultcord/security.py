"""Encryption and key-derivation helpers."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class CryptoError(RuntimeError):
    """Raised when decryption or cryptographic operations fail."""


def _derive_key(password: str, salt: bytes, length: int = 32) -> bytes:
    kdf = Scrypt(salt=salt, length=length, n=2**14, r=8, p=1)
    return kdf.derive(password.encode("utf-8"))


def encrypt_token(token: str, password: str) -> dict[str, str]:
    salt = secrets.token_bytes(16)
    key = _derive_key(password=password, salt=salt)
    fernet_key = base64.urlsafe_b64encode(key)
    cipher = Fernet(fernet_key)
    encrypted = cipher.encrypt(token.encode("utf-8"))
    return {
        "encrypted_token": base64.urlsafe_b64encode(encrypted).decode("utf-8"),
        "token_salt_b64": base64.urlsafe_b64encode(salt).decode("utf-8"),
        "kdf": "scrypt",
    }


def decrypt_token(payload: dict[str, str], password: str) -> str:
    try:
        salt = base64.urlsafe_b64decode(payload["token_salt_b64"].encode("utf-8"))
        encrypted = base64.urlsafe_b64decode(payload["encrypted_token"].encode("utf-8"))
        key = _derive_key(password=password, salt=salt)
        fernet_key = base64.urlsafe_b64encode(key)
        token = Fernet(fernet_key).decrypt(encrypted)
        return token.decode("utf-8")
    except (KeyError, InvalidToken, ValueError) as exc:
        raise CryptoError("Unable to decrypt token. Check your password.") from exc


def encrypt_message_payload(data: dict[str, Any], password: str) -> dict[str, str]:
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(password=password, salt=salt)
    aes = AESGCM(key)
    plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
    encrypted = aes.encrypt(nonce=nonce, data=plaintext, associated_data=None)
    return {
        "ciphertext_b64": base64.urlsafe_b64encode(encrypted).decode("utf-8"),
        "nonce_b64": base64.urlsafe_b64encode(nonce).decode("utf-8"),
        "salt_b64": base64.urlsafe_b64encode(salt).decode("utf-8"),
        "alg": "aesgcm+scrypt",
    }


def decrypt_message_payload(payload: dict[str, str], password: str) -> dict[str, Any]:
    try:
        salt = base64.urlsafe_b64decode(payload["salt_b64"].encode("utf-8"))
        nonce = base64.urlsafe_b64decode(payload["nonce_b64"].encode("utf-8"))
        ciphertext = base64.urlsafe_b64decode(payload["ciphertext_b64"].encode("utf-8"))
        key = _derive_key(password=password, salt=salt)
        aes = AESGCM(key)
        plaintext = aes.decrypt(nonce=nonce, data=ciphertext, associated_data=None)
        return json.loads(plaintext.decode("utf-8"))
    except (KeyError, InvalidTag, ValueError, json.JSONDecodeError) as exc:
        raise CryptoError("Unable to decrypt archived message.") from exc
