from vaultcord.security import CryptoError, decrypt_message_payload, decrypt_token, encrypt_message_payload, encrypt_token


def test_token_round_trip() -> None:
    payload = encrypt_token("abc123", "secret")
    token = decrypt_token(payload, "secret")
    assert token == "abc123"


def test_token_wrong_password_fails() -> None:
    payload = encrypt_token("abc123", "secret")
    try:
        decrypt_token(payload, "wrong")
    except CryptoError:
        pass
    else:
        raise AssertionError("Expected CryptoError")


def test_message_round_trip_uses_password() -> None:
    payload = {"content": "hello", "attachments": []}
    encrypted_a = encrypt_message_payload(payload, "pw")
    encrypted_b = encrypt_message_payload(payload, "pw")

    assert encrypted_a["salt_b64"] != encrypted_b["salt_b64"]

    decrypted = decrypt_message_payload(encrypted_a, "pw")
    assert decrypted["content"] == "hello"
