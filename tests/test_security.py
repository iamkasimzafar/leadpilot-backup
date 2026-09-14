"""Unit tests for password hashing and JWT handling."""

from datetime import timedelta

import pytest
from jwt.exceptions import InvalidTokenError

from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_password_round_trip() -> None:
    hashed = hash_password("s3cret-password")

    assert hashed != "s3cret-password"
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_access_token_round_trip() -> None:
    token = create_token("user-123", token_type="access")
    payload = decode_token(token, expected_type="access")

    assert payload["sub"] == "user-123"
    assert payload["type"] == "access"


def test_token_type_is_enforced() -> None:
    refresh = create_token("user-123", token_type="refresh")

    with pytest.raises(InvalidTokenError):
        decode_token(refresh, expected_type="access")


def test_expired_token_is_rejected() -> None:
    token = create_token("user-123", expires_delta=timedelta(seconds=-10))

    with pytest.raises(InvalidTokenError):
        decode_token(token)
