"""Argon2id password hashing and memory-bounded verification.

Each Argon2id verification with the argon2-cffi defaults allocates its 64 MiB
memory cost. Measured in the API container (2026-11-20): ~68 MiB baseline RSS,
+64 MiB per concurrent verification, 8 concurrent peaked at 391 MiB, above the
384 MiB container limit. ``PasswordVerifier`` therefore bounds how many
verifications run at once in this process; excess callers wait up to a queue
timeout and are then refused rather than allocating more memory.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
# Verified when the user or tenant does not exist so that failure timing is similar.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")

# Argon2 memory cost per verification, in MiB (argon2-cffi default memory_cost=65536 KiB).
ARGON2_MEMORY_MIB = _hasher.memory_cost // 1024


def hash_password(password: str) -> str:
    return str(_hasher.hash(password))


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return bool(_hasher.verify(password_hash or _DUMMY_HASH, password)) and (
            password_hash is not None
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


class VerifierBusyError(Exception):
    """No verification slot became free within the queue timeout."""


class PasswordVerifier:
    """Runs password verification with a process-wide concurrency bound."""

    def __init__(
        self,
        *,
        concurrency: int,
        queue_timeout_seconds: float,
        verify: Callable[[str | None, str], bool] = verify_password,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._slots = threading.BoundedSemaphore(concurrency)
        self._timeout = queue_timeout_seconds
        self._verify = verify
        self.concurrency = concurrency

    def verify(self, password_hash: str | None, password: str) -> bool:
        if not self._slots.acquire(timeout=self._timeout):
            raise VerifierBusyError
        try:
            return self._verify(password_hash, password)
        finally:
            self._slots.release()
