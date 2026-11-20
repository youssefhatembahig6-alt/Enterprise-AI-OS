"""Argon2id password hashing."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
# Verified when the user or tenant does not exist so that failure timing is similar.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


def hash_password(password: str) -> str:
    return str(_hasher.hash(password))


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return bool(_hasher.verify(password_hash or _DUMMY_HASH, password)) and (
            password_hash is not None
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
