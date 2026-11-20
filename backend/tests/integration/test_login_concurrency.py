"""Review finding 2: atomic admission before hashing and bounded verification.

Uses the real IdentityService, real Redis and the real PostgreSQL test database.
Verification functions are wrapped to observe overlap; they still call the real
Argon2 verifier unless a test needs a gate or a failure.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import Engine

from app.config import Settings
from app.repositories import identity as identity_repo
from app.security.passwords import PasswordVerifier, verify_password
from app.security.ratelimit import LoginRateLimiter
from app.services.identity import IdentityService, LoginBusy, LoginFailed, LoginRateLimited
from tests.integration.conftest import DbHarness, make_settings

CLIENT = "198.51.100.7"


class Tracker:
    """Wraps a verify function and records how many calls overlap."""

    def __init__(self, inner: Callable[[str | None, str], bool] = verify_password) -> None:
        self.inner = inner
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered = 0
        self.gate: threading.Event | None = None

    def __call__(self, password_hash: str | None, password: str) -> bool:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                assert self.gate.wait(timeout=10), "gate never released"
            return self.inner(password_hash, password)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def svc_settings(test_db: DbHarness, stub_http_url: str) -> Settings:
    return make_settings(test_db, stub_http_url)


@pytest.fixture
def redis_conn(svc_settings: Settings) -> Iterator[Redis]:
    client = Redis.from_url(svc_settings.redis_url.get_secret_value())
    yield client
    client.close()  # type: ignore[no-untyped-call]


def build(
    app_engine: Engine,
    settings: Settings,
    redis_conn: Redis,
    tracker: Tracker,
    *,
    concurrency: int,
    queue_timeout: float = 5.0,
) -> tuple[IdentityService, LoginRateLimiter]:
    limiter = LoginRateLimiter(
        redis_conn,
        prefix=settings.rate_limit_key_prefix,
        max_per_identity=settings.login_max_failures_per_identity,
        max_per_client=settings.login_max_failures_per_client,
        window_seconds=settings.login_failure_window_seconds,
    )
    verifier = PasswordVerifier(
        concurrency=concurrency, queue_timeout_seconds=queue_timeout, verify=tracker
    )
    return IdentityService(app_engine, settings, limiter, verifier), limiter


def counters(
    redis_conn: Redis, limiter: LoginRateLimiter, slug: str, email: str
) -> tuple[int, int]:
    keys = limiter._keys(slug, email, CLIENT)
    raw: list[Any] = list(redis_conn.mget([keys.identity_key, keys.client_key]))  # type: ignore[arg-type]
    return int(raw[0] or 0), int(raw[1] or 0)


def attempt(service: IdentityService, slug: str, email: str, password: str) -> str:
    try:
        service.login(
            tenant_slug=slug, email=email, password=password, client=CLIENT, request_id="t"
        )
        return "ok"
    except LoginFailed:
        return "failed"
    except LoginRateLimited:
        return "limited"
    except LoginBusy:
        return "busy"


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_overlapping_failures_cannot_exceed_identity_allowance(
    app_engine: Engine, svc_settings: Settings, redis_conn: Redis, test_db: DbHarness
) -> None:
    """12 simultaneous wrong-password requests: exactly 5 may reach Argon2."""

    tracker = Tracker()
    tracker.gate = threading.Event()
    service, limiter = build(app_engine, svc_settings, redis_conn, tracker, concurrency=16)
    email = test_db.foundation.tenants["niletech"].users["sales-reader"].email
    allowance = svc_settings.login_max_failures_per_identity

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(attempt, service, "niletech", email, "wrong-password") for _ in range(12)
        ]
        # Every request is either holding a verification slot or already refused,
        # while the admitted ones are still blocked inside verification.
        wait_until(lambda: tracker.entered + sum(f.done() for f in futures) == 12)
        assert tracker.entered == allowance
        assert tracker.active == allowance
        assert counters(redis_conn, limiter, "niletech", email)[0] == allowance
        tracker.gate.set()
        results = [f.result() for f in futures]

    assert results.count("failed") == allowance
    assert results.count("limited") == 12 - allowance
    assert attempt(service, "niletech", email, test_db.password("niletech/sales-reader")) == (
        "limited"
    )


def test_simultaneous_legitimate_sign_ins_are_bounded_and_all_succeed(
    app_engine: Engine, svc_settings: Settings, redis_conn: Redis, test_db: DbHarness
) -> None:
    def slow_verify(password_hash: str | None, password: str) -> bool:
        time.sleep(0.05)  # widen the overlap window
        return verify_password(password_hash, password)

    tracker = Tracker(slow_verify)
    service, limiter = build(app_engine, svc_settings, redis_conn, tracker, concurrency=2)
    refs = [
        "niletech/finance-reader",
        "niletech/sales-reader",
        "niletech/denied-reader",
        "niletech/board-member",
        "niletech/finance-admin",
        "niletech/access-admin",
    ]
    users = test_db.foundation.tenants["niletech"].users

    with ThreadPoolExecutor(max_workers=len(refs)) as pool:
        results = list(
            pool.map(
                lambda ref: attempt(
                    service, "niletech", users[ref.split("/")[1]].email, test_db.password(ref)
                ),
                refs,
            )
        )

    assert results == ["ok"] * len(refs)
    assert tracker.entered == len(refs)
    assert tracker.max_active == 2, "verification concurrency must stay at the bound"
    for ref in refs:
        assert counters(redis_conn, limiter, "niletech", users[ref.split("/")[1]].email) == (0, 0)


def test_busy_verifier_refuses_and_refunds_reservation(
    app_engine: Engine, svc_settings: Settings, redis_conn: Redis, test_db: DbHarness
) -> None:
    tracker = Tracker()
    tracker.gate = threading.Event()
    service, limiter = build(
        app_engine, svc_settings, redis_conn, tracker, concurrency=1, queue_timeout=0.3
    )
    users = test_db.foundation.tenants["niletech"].users
    holder_email, waiter_email = users["finance-reader"].email, users["board-member"].email

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(
            attempt, service, "niletech", holder_email, test_db.password("niletech/finance-reader")
        )
        wait_until(lambda: tracker.active == 1)
        waiter = attempt(
            service, "niletech", waiter_email, test_db.password("niletech/board-member")
        )
        assert waiter == "busy"
        # The refused attempt left no reservation behind; only the holder's remains.
        assert counters(redis_conn, limiter, "niletech", waiter_email) == (0, 1)
        tracker.gate.set()
        assert holder.result() == "ok"

    tracker.gate = None
    assert counters(redis_conn, limiter, "niletech", holder_email) == (0, 0)
    assert (
        attempt(service, "niletech", waiter_email, test_db.password("niletech/board-member"))
        == "ok"
    )


def test_verifier_error_releases_slot_and_reservation(
    app_engine: Engine, svc_settings: Settings, redis_conn: Redis, test_db: DbHarness
) -> None:
    calls = {"n": 0}

    def flaky(password_hash: str | None, password: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated verifier crash")
        return verify_password(password_hash, password)

    tracker = Tracker(flaky)
    service, limiter = build(app_engine, svc_settings, redis_conn, tracker, concurrency=1)
    email = test_db.foundation.tenants["isolation-test"].users["finance-admin"].email
    password = test_db.password("isolation-test/finance-admin")

    with pytest.raises(RuntimeError):
        service.login(
            tenant_slug="isolation-test",
            email=email,
            password=password,
            client=CLIENT,
            request_id="t",
        )
    assert counters(redis_conn, limiter, "isolation-test", email) == (0, 0)
    assert tracker.active == 0
    # With concurrency 1, a leaked slot would make this time out as "busy".
    assert attempt(service, "isolation-test", email, password) == "ok"


def test_lookup_error_after_admission_refunds_reservation(
    app_engine: Engine,
    svc_settings: Settings,
    redis_conn: Redis,
    test_db: DbHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = Tracker()
    service, limiter = build(app_engine, svc_settings, redis_conn, tracker, concurrency=1)
    email = test_db.foundation.tenants["isolation-test"].users["sales-reader"].email

    def broken_lookup(*_: Any, **__: Any) -> None:
        raise ConnectionError("simulated database outage")

    monkeypatch.setattr(identity_repo, "lookup_login", broken_lookup)
    with pytest.raises(ConnectionError):
        service.login(
            tenant_slug="isolation-test", email=email, password="x", client=CLIENT, request_id="t"
        )
    assert tracker.entered == 0
    assert counters(redis_conn, limiter, "isolation-test", email) == (0, 0)


def test_busy_maps_to_generic_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    service: IdentityService = client.app.state.identity  # type: ignore[attr-defined]

    def busy(**_: Any) -> None:
        raise LoginBusy

    monkeypatch.setattr(service, "login", busy)
    response = client.post(
        "/api/v1/auth/login", json={"tenant": "niletech", "email": "a@b.example", "password": "pw"}
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "service_unavailable"


def test_api_default_verifier_bound_matches_memory_budget(client: TestClient) -> None:
    service: IdentityService = client.app.state.identity  # type: ignore[attr-defined]
    assert service._verifier.concurrency == 2
