"""Reservation accounting in the login rate limiter, against real Redis.

The tests drive reservations directly in a fixed order (no timing races) and
read the outcome back through later admissions, not just internal counters.
One test repeats the overlap case through the real IdentityService, with
verification gated so the order is controlled.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from redis import Redis
from sqlalchemy import Engine

from app.security.passwords import PasswordVerifier, verify_password
from app.security.ratelimit import LoginRateLimiter, Reservation
from app.services.identity import IdentityService, LoginFailed
from tests.integration.conftest import REDIS_URL, DbHarness, make_settings

TENANT, EMAIL, CLIENT = "tenant-a", "person@tenant-a.example", "203.0.113.9"
MAX_IDENTITY, MAX_CLIENT = 3, 10


@pytest.fixture
def redis_conn() -> Iterator[Redis]:
    assert REDIS_URL
    client = Redis.from_url(REDIS_URL)
    yield client
    client.close()  # type: ignore[no-untyped-call]


@pytest.fixture
def limiter(redis_conn: Redis) -> LoginRateLimiter:
    return LoginRateLimiter(
        redis_conn,
        prefix=f"eaios-test:{uuid.uuid4().hex}",
        max_per_identity=MAX_IDENTITY,
        max_per_client=MAX_CLIENT,
        window_seconds=600,
    )


def admit(limiter: LoginRateLimiter, email: str = EMAIL, client: str = CLIENT) -> Reservation:
    reservation = limiter.reserve(TENANT, email, client)
    assert isinstance(reservation, Reservation), f"unexpectedly refused (retry {reservation})"
    return reservation


def remaining_identity_admissions(limiter: LoginRateLimiter, email: str = EMAIL) -> int:
    """Admit failing attempts from fresh clients until refused; return how many fit."""

    for admitted in range(MAX_IDENTITY + 2):
        reservation = limiter.reserve(TENANT, email, f"198.51.100.{admitted}")
        if not isinstance(reservation, Reservation):
            return admitted
        limiter.settle_failure(reservation)
    raise AssertionError("identity limit never reached")


def remaining_client_admissions(limiter: LoginRateLimiter, client: str = CLIENT) -> int:
    """Admit failing attempts for fresh identities until refused; return how many fit."""

    for admitted in range(MAX_CLIENT + 2):
        reservation = limiter.reserve(TENANT, f"probe{admitted}@tenant-a.example", client)
        if not isinstance(reservation, Reservation):
            return admitted
        limiter.settle_failure(reservation)
    raise AssertionError("client limit never reached")


def expire_now(redis_conn: Redis, *keys: str) -> None:
    for key in keys:
        assert redis_conn.pexpire(key, 1)
    deadline = time.monotonic() + 5
    while redis_conn.exists(*keys):
        assert time.monotonic() < deadline, "window did not expire"
        time.sleep(0.005)


# 1 — overlapping attempts: success must not erase the pending attempt's reservation.


def test_success_keeps_overlapping_reservation_and_its_failure_counts(
    limiter: LoginRateLimiter,
) -> None:
    earlier = admit(limiter)
    limiter.settle_failure(earlier)  # one settled failure before the overlap
    first = admit(limiter)
    second = admit(limiter)  # pending while the first succeeds
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == 3

    limiter.settle_success(first)
    # Earlier failures are cleared, but the pending second attempt still holds its unit.
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == 1

    limiter.settle_failure(second)
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == 1
    # Verified through admissions: only MAX_IDENTITY - 1 further attempts fit.
    assert remaining_identity_admissions(limiter) == MAX_IDENTITY - 1


def test_overlap_through_identity_service(
    test_db: DbHarness, stub_http_url: str, app_engine: Engine, redis_conn: Redis
) -> None:
    """Same case end to end: A (correct) succeeds while B (wrong) is in verification."""

    settings = make_settings(test_db, stub_http_url, LOGIN_MAX_FAILURES_PER_IDENTITY=MAX_IDENTITY)
    limiter = LoginRateLimiter(
        redis_conn,
        prefix=settings.rate_limit_key_prefix,
        max_per_identity=MAX_IDENTITY,
        max_per_client=MAX_CLIENT,
        window_seconds=600,
    )
    good = test_db.password("niletech/finance-admin")
    email = test_db.foundation.tenants["niletech"].users["finance-admin"].email
    gates = {good: threading.Event(), "wrong-password": threading.Event()}
    entered = {good: threading.Event(), "wrong-password": threading.Event()}

    def gated_verify(password_hash: str | None, password: str) -> bool:
        entered[password].set()
        assert gates[password].wait(timeout=10)
        return verify_password(password_hash, password)

    service = IdentityService(
        app_engine,
        settings,
        limiter,
        PasswordVerifier(concurrency=2, queue_timeout_seconds=10, verify=gated_verify),
    )

    def login(password: str) -> str:
        try:
            service.login(
                tenant_slug="niletech",
                email=email,
                password=password,
                client=CLIENT,
                request_id="t",
            )
            return "ok"
        except LoginFailed:
            return "failed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(login, good)
        second = pool.submit(login, "wrong-password")
        assert entered[good].wait(10) and entered["wrong-password"].wait(10)
        gates[good].set()
        assert first.result() == "ok"
        assert limiter.usage("niletech", email, CLIENT).identity == 1  # B still pending
        gates["wrong-password"].set()
        assert second.result() == "failed"

    assert limiter.usage("niletech", email, CLIENT).identity == 1
    admitted = 0
    for i in range(MAX_IDENTITY + 2):
        reservation = limiter.reserve("niletech", email, f"192.0.2.{i}")
        if not isinstance(reservation, Reservation):
            break
        limiter.settle_failure(reservation)
        admitted += 1
    assert admitted == MAX_IDENTITY - 1


# 2 — a refund applies only to the reservation's original window.


def test_refund_after_window_expiry_does_not_touch_new_window(
    limiter: LoginRateLimiter, redis_conn: Redis
) -> None:
    old = admit(limiter)
    expire_now(redis_conn, old.identity_key, old.client_key)

    new = admit(limiter)  # starts the next window for both keys
    assert new.identity_window != old.identity_window
    assert new.client_window != old.client_window
    before = limiter.usage(TENANT, EMAIL, CLIENT)
    assert (before.identity, before.client) == (1, 1)

    limiter.release(old)
    limiter.settle_failure(old)
    limiter.settle_success(old)

    after = limiter.usage(TENANT, EMAIL, CLIENT)
    assert (after.identity, after.client) == (1, 1)
    # Allowances in the new window are exactly what one pending attempt leaves.
    assert remaining_client_admissions(limiter) == MAX_CLIENT - 1
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == 1
    limiter.release(new)
    assert remaining_identity_admissions(limiter) == MAX_IDENTITY


# 3 — a refund applies at most once.


def test_double_refund_preserves_other_pending_reservation(limiter: LoginRateLimiter) -> None:
    refunded = admit(limiter)
    pending = admit(limiter)
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == 2

    limiter.release(refunded)
    limiter.release(refunded)  # duplicate refund must be a no-op

    usage = limiter.usage(TENANT, EMAIL, CLIENT)
    assert (usage.identity, usage.client) == (1, 1), "pending attempt lost its reservation"
    # The pending attempt's allowance is intact: it still occupies one identity unit...
    assert remaining_identity_admissions(limiter) == MAX_IDENTITY - 1
    # ...and when it fails, that failure is still counted exactly once.
    limiter.settle_failure(pending)
    limiter.settle_failure(pending)
    assert limiter.usage(TENANT, EMAIL, CLIENT).identity == MAX_IDENTITY


def test_admission_remains_atomic_under_overlap(limiter: LoginRateLimiter) -> None:
    """Concurrent reserve calls never admit more than the identity limit."""

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: limiter.reserve(TENANT, EMAIL, CLIENT), range(12)))
    admitted = [r for r in results if isinstance(r, Reservation)]
    assert len(admitted) == MAX_IDENTITY
    assert len({r.field for r in admitted}) == MAX_IDENTITY


def test_leftover_v1_string_counters_do_not_break_admission(
    limiter: LoginRateLimiter, redis_conn: Redis
) -> None:
    """After upgrading, string counters from the previous layout must be ignored."""

    import hashlib

    prefix = limiter._prefix
    legacy_identity = (
        f"{prefix}:login:id:" + hashlib.sha256(f"{TENANT}\x1f{EMAIL}".encode()).hexdigest()
    )
    legacy_client = f"{prefix}:login:client:" + hashlib.sha256(CLIENT.encode()).hexdigest()
    redis_conn.set(legacy_identity, 99, ex=600)
    redis_conn.set(legacy_client, 99, ex=600)

    reservation = admit(limiter)
    assert reservation.identity_key not in (legacy_identity, legacy_client)
    limiter.settle_failure(reservation)
    usage = limiter.usage(TENANT, EMAIL, CLIENT)
    assert (usage.identity, usage.client) == (1, 1)
    assert redis_conn.get(legacy_identity) == b"99", "legacy key left to expire untouched"
