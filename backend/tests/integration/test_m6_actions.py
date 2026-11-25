"""M6 acceptance: verified report -> email draft -> human approval -> outbox -> Mailpit,
plus scheduled briefings that only ever produce drafts for review.

Real PostgreSQL (RLS, app role), real Redis and a real Mailpit server (SMTP + HTTP API).
Report runs use the explicit fake model double (labelled ``fake``): this is integration
evidence for the action flow, not model-quality evidence.
"""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.actions.mail import Accepted, Ambiguous, MailpitLookup, SmtpMailer
from app.db.engine import create_app_engine, tenant_transaction
from app.main import create_app
from tests.integration.conftest import (
    DbHarness,
    disposable_database,
    login,
    owner_exec,
    report_settings,
    tenant_packs,
)
from tests.integration.report_support import ask, auth, wait_for_run

SMTP_HOST = os.environ.get("TEST_MAILPIT_SMTP_HOST")
SMTP_PORT = int(os.environ.get("TEST_MAILPIT_SMTP_PORT", "1025"))
MAILPIT_API = os.environ.get("TEST_MAILPIT_API_URL")
Q1 = "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?"
FINANCE = "niletech/finance-reader"
Q1_NET = "80,934,385.00"


class SimulatedCrash(BaseException):
    """Raised from a fault-injection hook: nothing after it in the worker runs."""


# -- fixtures --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def mailpit() -> tuple[str, int, str]:
    if not (SMTP_HOST and MAILPIT_API):
        message = "TEST_MAILPIT_SMTP_HOST and TEST_MAILPIT_API_URL are required for M6 tests"
        if os.environ.get("EAIOS_REQUIRE_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return SMTP_HOST, SMTP_PORT, MAILPIT_API


@pytest.fixture(scope="session")
def actions_db() -> Iterator[DbHarness]:
    """Own disposable database (these tests change memberships, grants and statuses)."""

    from app.business_data.loader import import_sales_dataset
    from app.business_data.model import normalize
    from app.business_data.source import verify_vendored

    with disposable_database(seed=True) as db:
        source = verify_vendored()
        import_sales_dataset(db.owner_engine, source, normalize(source), environment="test")
        yield db


@pytest.fixture
def make_client(
    actions_db: DbHarness, stub_http_url: str, mailpit: tuple[str, int, str]
) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []
    host, port, api = mailpit

    def build(mailer: Any = None, lookup: Any = None, **overrides: Any) -> TestClient:
        values = {
            "SMTP_HOST": host,
            "SMTP_PORT": port,
            "MAILPIT_API_URL": api,
            "EMAIL_DISPATCH_LEASE_SECONDS": 1,
            **overrides,
        }
        app = create_app(
            report_settings(actions_db, stub_http_url, **values),
            mailer=mailer,
            mail_lookup=lookup,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def app_state(client: TestClient) -> Any:
    """The app's state (delivery hooks, worker, service), for fault injection."""

    return client.app.state  # type: ignore[attr-defined]


def user_id(db: DbHarness, ref: str) -> str:
    slug, key = ref.split("/", 1)
    return str(db.foundation.tenants[slug].users[key].id)


@contextmanager
def activated(db: DbHarness, ref: str) -> Iterator[str]:
    """The fixture's inactive finance reader, temporarily active: an authorized second
    recipient with the same dataset access as the finance reader."""

    uid = user_id(db, ref)
    owner_exec(db, "UPDATE users SET status = 'active' WHERE id = :u", {"u": uid})
    try:
        yield uid
    finally:
        owner_exec(db, "UPDATE users SET status = 'inactive' WHERE id = :u", {"u": uid})


@contextmanager
def without_finance_team(db: DbHarness, ref: str) -> Iterator[None]:
    tenant = db.foundation.tenants["niletech"]
    group, uid = tenant.groups["finance-team"].id, user_id(db, ref)
    owner_exec(
        db,
        "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u",
        {"g": group, "u": uid},
    )
    try:
        yield
    finally:
        owner_exec(
            db,
            "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)"
            " ON CONFLICT DO NOTHING",
            {"t": tenant.id, "g": group, "u": uid},
        )


RECIPIENT = "niletech/inactive-reader"


@pytest.fixture
def recipient(actions_db: DbHarness) -> Iterator[str]:
    with activated(actions_db, RECIPIENT) as uid:
        yield uid


def recipient_email(db: DbHarness) -> str:
    return db.foundation.tenants["niletech"].users["inactive-reader"].email


def publish_q1(client: TestClient, token: str) -> str:
    stream = ask(client, token, Q1)
    assert stream.done["outcome"] == "published", stream.raw[-400:]
    assert stream.run_id
    return stream.run_id


def create_draft(client: TestClient, token: str, run_id: str, to: str) -> dict[str, Any]:
    r = client.post(
        "/api/v1/actions/email-drafts",
        json={"source_run_id": run_id, "recipient_user_id": to},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def approve(client: TestClient, token: str, draft: dict[str, Any]) -> dict[str, Any]:
    r = client.post(
        f"/api/v1/actions/email-drafts/{draft['action_id']}/approve",
        json={"version": draft["version"], "draft_sha256": draft["draft_sha256"]},
        headers=auth(token),
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def send(
    client: TestClient, token: str, draft: dict[str, Any], approval_token: str, **over: Any
) -> httpx.Response:
    body = {
        "version": draft["version"],
        "draft_sha256": draft["draft_sha256"],
        "approval_token": approval_token,
        **over,
    }
    return client.post(
        f"/api/v1/actions/email-drafts/{draft['action_id']}/send", json=body, headers=auth(token)
    )


def outbox(db: DbHarness, action_id: str) -> Any:
    rows = owner_exec(
        db,
        "SELECT * FROM email_outbox WHERE action_id = :a",
        {"a": action_id},
    ).all()
    return rows[0] if rows else None


def wait_outbox(db: DbHarness, action_id: str, statuses: set[str], timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = outbox(db, action_id)
        if row is not None and row.status in statuses:
            return row
        time.sleep(0.05)
    raise AssertionError(f"outbox never reached {statuses}: {outbox(db, action_id)}")


def mailpit_messages(api: str, message_id: str) -> list[dict[str, Any]]:
    bare = message_id.strip("<>")
    r = httpx.get(f"{api}/api/v1/search", params={"query": f"message-id:{bare}"}, timeout=5)
    r.raise_for_status()
    return [m for m in r.json()["messages"] if m["MessageID"] == bare]


def mailpit_body(api: str, mailpit_id: str) -> dict[str, Any]:
    r = httpx.get(f"{api}/api/v1/message/{mailpit_id}", timeout=5)
    r.raise_for_status()
    body: dict[str, Any] = r.json()
    return body


def audit_reasons(db: DbHarness, action_id: str) -> list[str]:
    rows = owner_exec(
        db,
        "SELECT action, decision, reason FROM audit_events"
        " WHERE details->>'action_id' = :a OR details->>'subject_id' = :a ORDER BY occurred_at",
        {"a": action_id},
    ).all()
    return [f"{r.action}:{r.decision}:{r.reason}" for r in rows]


def flow_to_approval(
    client: TestClient, db: DbHarness, to: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    token = login(client, db, FINANCE)
    run_id = publish_q1(client, token)
    draft = create_draft(client, token, run_id, to)
    return token, draft, approve(client, token, draft)


# -- the complete flow -----------------------------------------------------------------


def test_verified_report_to_draft_to_approval_to_mailpit(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    run_id = publish_q1(client, token)

    draft = create_draft(client, token, run_id, recipient)
    assert draft["status"] == "draft" and draft["version"] == 1 and not draft["edited"]
    assert draft["recipient"]["email"] == recipient_email(actions_db)
    assert Q1_NET in draft["body"] and "Verification: figures_verified" in draft["body"]
    assert draft["delivery"] is None

    # Nothing is queued before approval.
    assert outbox(actions_db, draft["action_id"]) is None
    approval = approve(client, token, draft)
    assert approval["version"] == 1 and approval["draft_sha256"] == draft["draft_sha256"]
    stored = owner_exec(
        actions_db,
        "SELECT token_sha256, actor_user_id, session_id FROM email_approvals WHERE action_id = :a",
        {"a": draft["action_id"]},
    ).one()
    assert approval["approval_token"] not in str(stored)  # only the hash is stored
    assert str(stored.actor_user_id) == user_id(actions_db, FINANCE)

    r = send(client, token, draft, approval["approval_token"])
    assert r.status_code == 202, r.text
    row = wait_outbox(actions_db, draft["action_id"], {"delivered"})
    assert row.attempts == 1 and row.status_reason == "smtp_accepted"
    assert row.message_id == f"<{row.execution_key}@eaios.local>"

    (message,) = mailpit_messages(mailpit[2], row.message_id)
    assert [a["Address"] for a in message["To"]] == [recipient_email(actions_db)]
    assert message["Subject"] == draft["subject"]
    full = mailpit_body(mailpit[2], message["ID"])
    assert Q1_NET in full["Text"]

    final = client.get(f"/api/v1/actions/email-drafts/{draft['action_id']}", headers=auth(token))
    assert final.json()["status"] == "delivered"
    assert final.json()["delivery"]["status"] == "delivered"
    trail = audit_reasons(actions_db, draft["action_id"])
    for expected in (
        "email.draft:info:draft_created",
        "email.approve:allow:approved",
        "email.send:allow:queued",
        "email.deliver:allow:claimed",
        "email.deliver:allow:smtp_accepted",
    ):
        assert expected in trail, trail


# -- approval binding ------------------------------------------------------------------


def test_forged_approval_is_rejected(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token, draft, _ = flow_to_approval(client, actions_db, recipient)
    r = send(client, token, draft, secrets.token_urlsafe(32))
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_rejected"
    assert outbox(actions_db, draft["action_id"]) is None
    assert "email.send:deny:approval_unknown" in audit_reasons(actions_db, draft["action_id"])


def test_expired_approval_is_rejected(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    owner_exec(
        actions_db,
        "UPDATE email_approvals SET created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour' WHERE action_id = :a",
        {"a": draft["action_id"]},
    )
    r = send(client, token, draft, approval["approval_token"])
    assert r.status_code == 409 and "expired" in r.json()["error"]["message"]
    assert outbox(actions_db, draft["action_id"]) is None


def test_same_version_can_be_approved_again_after_expiry(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    token, draft, first = flow_to_approval(client, actions_db, recipient)
    owner_exec(
        actions_db,
        "UPDATE email_approvals SET created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour' WHERE action_id = :a",
        {"a": draft["action_id"]},
    )
    shown = client.get(f"/api/v1/actions/email-drafts/{draft['action_id']}", headers=auth(token))
    assert shown.json()["approval_expires_at"] is None  # the UI offers "Approve" again
    assert send(client, token, draft, first["approval_token"]).status_code == 409
    second = approve(client, token, draft)  # same version, same hash
    assert second["version"] == draft["version"] == 1
    assert second["approval_token"] != first["approval_token"]
    assert send(client, token, draft, first["approval_token"]).status_code == 409
    assert send(client, token, draft, second["approval_token"]).status_code == 202
    row = wait_outbox(actions_db, draft["action_id"], {"delivered"})
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1
    statuses = (
        owner_exec(
            actions_db,
            "SELECT status FROM email_approvals WHERE action_id = :a ORDER BY created_at",
            {"a": draft["action_id"]},
        )
        .scalars()
        .all()
    )
    assert statuses == ["invalidated", "consumed"]


def test_reused_approval_is_rejected(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    again = send(client, token, draft, approval["approval_token"])
    assert again.status_code == 409 and "already used" in again.json()["error"]["message"]
    row = wait_outbox(actions_db, draft["action_id"], {"delivered"})
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1
    assert "email.send:deny:approval_used" in audit_reasons(actions_db, draft["action_id"])


def test_approval_is_bound_to_actor_session_and_tenant(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    action = draft["action_id"]

    # Same user, different session: the approval belongs to the session that reviewed.
    other_session = login(client, actions_db, FINANCE)
    r = send(client, other_session, draft, approval["approval_token"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_rejected"

    # Another user of the same tenant cannot even see the draft.
    sales = login(client, actions_db, "niletech/sales-reader")
    r = send(client, sales, draft, approval["approval_token"])
    assert r.status_code == 404
    assert (
        client.get(f"/api/v1/actions/email-drafts/{action}", headers=auth(sales)).status_code == 404
    )

    # Another tenant: same 404, and RLS hides the approval row itself.
    foreign = login(client, actions_db, "isolation-test/finance-reader")
    r = send(client, foreign, draft, approval["approval_token"])
    assert r.status_code == 404
    engine = create_app_engine(actions_db.app_url, pool_size=1)
    try:
        iso = actions_db.foundation.tenants["isolation-test"].id
        with tenant_transaction(engine, iso) as conn:
            seen = conn.execute(
                text("SELECT count(*) FROM email_approvals WHERE action_id = :a"), {"a": action}
            ).scalar_one()
            outboxes = conn.execute(text("SELECT count(*) FROM email_outbox")).scalar_one()
        assert seen == 0 and outboxes == 0
    finally:
        engine.dispose()

    # The rightful session still works afterwards: rejections consumed nothing.
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    trail = audit_reasons(actions_db, action)
    assert "email.send:deny:approval_actor" in trail


def test_cross_tenant_approval_token_cannot_send_own_tenant_draft(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    """A valid niletech token presented against another niletech draft, and a draft id
    from niletech presented by another tenant, both fail without consuming anything."""

    client = make_client()
    token, first, approval = flow_to_approval(client, actions_db, recipient)
    run_id = owner_exec(
        actions_db,
        "SELECT source_run_id FROM email_actions WHERE id = :a",
        {"a": first["action_id"]},
    ).scalar_one()
    second = create_draft(client, token, str(run_id), recipient)
    r = send(client, token, second, approval["approval_token"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "approval_rejected"
    assert "email.send:deny:approval_mismatch" in audit_reasons(actions_db, second["action_id"])
    status = owner_exec(
        actions_db,
        "SELECT status FROM email_approvals WHERE action_id = :a",
        {"a": first["action_id"]},
    ).scalar_one()
    assert status == "active"


# -- edits -----------------------------------------------------------------------------


def test_edit_after_approval_requires_fresh_approval(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    token, draft, old = flow_to_approval(client, actions_db, recipient)
    edited = client.put(
        f"/api/v1/actions/email-drafts/{draft['action_id']}",
        json={
            "version": draft["version"],
            "draft_sha256": draft["draft_sha256"],
            "recipient_user_id": recipient,
            "subject": "Q1 2026 sales (reviewed)",
            "body": draft["body"] + "\n\nReviewed by finance before sending.",
        },
        headers=auth(token),
    )
    assert edited.status_code == 200, edited.text
    v2 = edited.json()
    assert v2["version"] == 2 and v2["edited"] and v2["status"] == "draft"
    assert v2["draft_sha256"] != draft["draft_sha256"]

    # The old approval is dead, with the old or the new version reference.
    for ref in (draft, v2):
        r = send(client, token, ref, old["approval_token"])
        assert r.status_code == 409 and r.json()["error"]["code"] == "approval_rejected"
    assert outbox(actions_db, draft["action_id"]) is None

    # Editing with a stale version is a conflict, not a silent overwrite.
    stale = client.put(
        f"/api/v1/actions/email-drafts/{draft['action_id']}",
        json={
            "version": 1,
            "draft_sha256": draft["draft_sha256"],
            "recipient_user_id": recipient,
            "subject": "x",
            "body": "y",
        },
        headers=auth(token),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"

    fresh = approve(client, token, v2)
    assert send(client, token, v2, fresh["approval_token"]).status_code == 202
    row = wait_outbox(actions_db, draft["action_id"], {"delivered"})
    assert row.version == 2
    (message,) = mailpit_messages(mailpit[2], row.message_id)
    assert message["Subject"] == "Q1 2026 sales (reviewed)"
    assert "Reviewed by finance" in mailpit_body(mailpit[2], message["ID"])["Text"]
    versions = owner_exec(
        actions_db,
        "SELECT version, edited_by IS NOT NULL AS edited FROM email_draft_versions"
        " WHERE action_id = :a ORDER BY version",
        {"a": draft["action_id"]},
    ).all()
    assert [(v.version, v.edited) for v in versions] == [(1, False), (2, True)]


def test_draft_versions_are_immutable_and_tampering_is_detected(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    with pytest.raises(Exception, match=r"immutable|not allowed|reject"):
        owner_exec(
            actions_db,
            "UPDATE email_draft_versions SET body = 'tampered' WHERE action_id = :a",
            {"a": draft["action_id"]},
        )
    # The app role cannot update versions at all.
    engine = create_app_engine(actions_db.app_url, pool_size=1)
    try:
        tenant = actions_db.foundation.tenants["niletech"].id
        with (
            pytest.raises(Exception, match="permission denied"),
            tenant_transaction(engine, tenant) as conn,
        ):
            conn.execute(
                text("UPDATE email_draft_versions SET body = 'x' WHERE action_id = :a"),
                {"a": draft["action_id"]},
            )
    finally:
        engine.dispose()
    assert send(client, token, draft, approval["approval_token"]).status_code == 202


def test_requests_cannot_supply_addresses_or_identity(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    run_id = publish_q1(client, token)
    for extra in (
        {"recipient_email": "attacker@evil.example"},
        {"tenant_id": str(uuid.uuid4())},
        {"owner_user_id": user_id(actions_db, "niletech/sales-reader")},
    ):
        r = client.post(
            "/api/v1/actions/email-drafts",
            json={"source_run_id": run_id, "recipient_user_id": recipient, **extra},
            headers=auth(token),
        )
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    foreign_user = user_id(actions_db, "isolation-test/finance-reader")
    r = client.post(
        "/api/v1/actions/email-drafts",
        json={"source_run_id": run_id, "recipient_user_id": foreign_user},
        headers=auth(token),
    )
    assert r.status_code == 422 and "not a user in your organization" in r.text
    draft = create_draft(client, token, run_id, recipient)
    r = client.put(
        f"/api/v1/actions/email-drafts/{draft['action_id']}",
        json={
            "version": 1,
            "draft_sha256": draft["draft_sha256"],
            "recipient_user_id": recipient,
            "subject": "Hi\r\nBcc: attacker@evil.example",
            "body": "x",
        },
        headers=auth(token),
    )
    assert r.status_code == 422
    # Another user's run cannot be a source.
    sales = login(client, actions_db, "niletech/sales-reader")
    r = client.post(
        "/api/v1/actions/email-drafts",
        json={"source_run_id": run_id, "recipient_user_id": recipient},
        headers=auth(sales),
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("ref", "reason"),
    [
        ("niletech/board-member", "recipient_not_authorized"),  # no dataset.query role
        ("niletech/denied-reader", "recipient_not_authorized"),  # explicit deny grant
    ],
)
def test_unauthorized_recipient_cannot_be_drafted(
    make_client: Callable[..., TestClient], actions_db: DbHarness, ref: str, reason: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    run_id = publish_q1(client, token)
    r = client.post(
        "/api/v1/actions/email-drafts",
        json={"source_run_id": run_id, "recipient_user_id": user_id(actions_db, ref)},
        headers=auth(token),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_blocked"
    rows = owner_exec(
        actions_db,
        "SELECT reason FROM audit_events WHERE action = 'email.draft' AND decision = 'deny'"
        " AND details->>'subject_id' = :r",
        {"r": run_id},
    ).all()
    assert reason in [row.reason for row in rows]


# -- revocation before send --------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    ["sender_loses_access", "recipient_loses_access", "recipient_deactivated", "pack_disabled"],
)
def test_permission_revocation_before_send_blocks_it(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    change: str,
) -> None:
    client = make_client()
    token, draft, approval = flow_to_approval(client, actions_db, recipient)

    @contextmanager
    def revoked() -> Iterator[None]:
        if change == "sender_loses_access":
            with without_finance_team(actions_db, FINANCE):
                yield
        elif change == "recipient_loses_access":
            with without_finance_team(actions_db, RECIPIENT):
                yield
        elif change == "recipient_deactivated":
            owner_exec(
                actions_db, "UPDATE users SET status = 'inactive' WHERE id = :u", {"u": recipient}
            )
            yield
        else:
            with tenant_packs(
                actions_db, "niletech", ["documents", "sales_reports", "legal", "code"]
            ):
                yield

    with revoked():
        r = send(client, token, draft, approval["approval_token"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_blocked", r.text
    assert outbox(actions_db, draft["action_id"]) is None
    state = owner_exec(
        actions_db,
        "SELECT a.status, a.status_reason, ap.status AS approval FROM email_actions a"
        " JOIN email_approvals ap ON ap.action_id = a.id WHERE a.id = :a",
        {"a": draft["action_id"]},
    ).one()
    assert state.status == "draft" and state.approval == "invalidated"
    # Once access is back, the old approval still cannot be used: a fresh one is needed.
    r = send(client, token, draft, approval["approval_token"])
    assert r.status_code == 409


def test_revocation_after_queueing_blocks_at_dispatch(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    app_state(client).actions.dispatch = None
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    with without_finance_team(actions_db, RECIPIENT):
        outcomes = app_state(client).action_worker.tick()
    assert "blocked" in outcomes["deliver"]
    row = outbox(actions_db, draft["action_id"])
    assert row.status == "blocked" and row.status_reason == "recipient_not_authorized"
    assert row.attempts == 0 and mailpit_messages(mailpit[2], row.message_id) == []


def test_cancel_before_dispatch(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    app_state(client).actions.dispatch = None
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    r = client.post(
        f"/api/v1/actions/email-drafts/{draft['action_id']}/cancel", headers=auth(token)
    )
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    app_state(client).action_worker.tick()
    row = outbox(actions_db, draft["action_id"])
    assert row.status == "cancelled" and mailpit_messages(mailpit[2], row.message_id) == []


def test_owner_revocation_withholds_draft_content(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    draft = create_draft(client, token, publish_q1(client, token), recipient)
    with without_finance_team(actions_db, FINANCE):
        r = client.get(f"/api/v1/actions/email-drafts/{draft['action_id']}", headers=auth(token))
        body = r.json()
        assert body["content_withheld"] and body["body"] is None and body["subject"] is None
        assert Q1_NET not in r.text and "80934385" not in r.text


# -- crash recovery and reconciliation -----------------------------------------------------


def _crash(row: Any) -> None:
    raise SimulatedCrash(str(row.execution_key))


def test_crash_after_claim_before_dispatch_is_reconciled_and_sent_once(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    state = app_state(client)
    state.email_delivery.hooks["before_smtp"] = _crash
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202

    row = wait_outbox(actions_db, draft["action_id"], {"dispatching"})
    assert row.attempts == 1 and mailpit_messages(mailpit[2], row.message_id) == []
    state.email_delivery.hooks.clear()
    time.sleep(1.2)  # lease (1 s in tests) expires

    worker = state.action_worker
    assert worker.tick()["stale"] == ["uncertain"]
    assert outbox(actions_db, draft["action_id"]).status == "uncertain"
    # No SMTP-start record for attempt 1: it provably never reached SMTP.
    assert smtp_attempts(actions_db, row.execution_key) == []
    assert worker.tick()["reconcile"] == ["requeued"]
    assert worker.tick()["deliver"] == ["delivered"]
    row = outbox(actions_db, draft["action_id"])
    assert row.status == "delivered" and row.attempts == 2
    assert smtp_attempts(actions_db, row.execution_key) == [2]
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1
    trail = audit_reasons(actions_db, draft["action_id"])
    assert "email.deliver:info:dispatch_lease_expired" in trail
    assert "email.deliver:info:never_reached_smtp_retry" in trail


def test_crash_after_mailpit_acceptance_is_reconciled_without_resending(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    state = app_state(client)
    state.email_delivery.hooks["after_accept"] = _crash
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202

    row = wait_outbox(actions_db, draft["action_id"], {"dispatching"})
    deadline = time.monotonic() + 10
    while not mailpit_messages(mailpit[2], row.message_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1  # accepted, not recorded
    assert outbox(actions_db, draft["action_id"]).confirmed_at is None
    state.email_delivery.hooks.clear()
    time.sleep(1.2)

    worker = state.action_worker
    assert worker.tick()["stale"] == ["uncertain"]
    assert worker.tick()["reconcile"] == ["delivered"]
    row = outbox(actions_db, draft["action_id"])
    assert row.status == "delivered" and row.status_reason == "reconciled_found"
    assert row.attempts == 1 and row.mailpit_id
    assert worker.tick().get("deliver") is None
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1  # never re-sent
    final = client.get(f"/api/v1/actions/email-drafts/{draft['action_id']}", headers=auth(token))
    assert final.json()["status"] == "delivered"


# -- review round 1: access lost after the claim, receipts, lock order ---------------------


def _membership(db: DbHarness, ref: str, present: bool) -> None:
    tenant = db.foundation.tenants["niletech"]
    params = {"t": tenant.id, "g": tenant.groups["finance-team"].id, "u": user_id(db, ref)}
    if present:
        owner_exec(
            db,
            "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)"
            " ON CONFLICT DO NOTHING",
            params,
        )
    else:
        owner_exec(db, "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u", params)


def queued(client: TestClient, db: DbHarness, to: str) -> tuple[str, dict[str, Any], Any]:
    """A send queued in the outbox but not dispatched (the test drives the worker)."""

    app_state(client).actions.dispatch = None
    token, draft, approval = flow_to_approval(client, db, to)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    return token, draft, outbox(db, draft["action_id"])


@pytest.mark.parametrize("who", [FINANCE, RECIPIENT])
def test_access_lost_after_claim_blocks_the_smtp_handoff(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
    who: str,
) -> None:
    client = make_client()
    _, draft, row = queued(client, actions_db, recipient)
    delivery = app_state(client).email_delivery
    claimed: list[Any] = []

    def revoke(claimed_row: Any) -> None:
        claimed.append(claimed_row)
        assert outbox(actions_db, draft["action_id"]).status == "dispatching"
        _membership(actions_db, who, present=False)

    delivery.hooks["before_smtp"] = revoke
    try:
        assert delivery.deliver(row.execution_key, row.tenant_id) == "blocked"
    finally:
        delivery.hooks.clear()
        _membership(actions_db, who, present=True)
    assert claimed  # the claim really committed first
    final = outbox(actions_db, draft["action_id"])
    expected = "source_access_revoked" if who == FINANCE else "recipient_not_authorized"
    assert final.status == "blocked" and final.status_reason == expected
    assert final.attempts == 1 and final.smtp_accepted_at is None
    assert mailpit_messages(mailpit[2], row.message_id) == []  # Mailpit received nothing
    assert f"email.deliver:deny:{expected}" in audit_reasons(actions_db, draft["action_id"])


class RevokeDuringSmtp:
    """Starts a revocation while the handoff is talking SMTP and checks it is held back
    by the handoff's policy-epoch lock until the outcome is recorded."""

    def __init__(self, inner: SmtpMailer, db: DbHarness) -> None:
        self.inner, self.db = inner, db
        self.thread: threading.Thread | None = None
        self.blocked_during_smtp = False
        self.membership_during_smtp: int | None = None

    def send(self, message: EmailMessage, sender: str, recipient: str) -> Accepted:
        self.thread = threading.Thread(
            target=_membership, args=(self.db, RECIPIENT, False), daemon=True
        )
        self.thread.start()
        self.thread.join(0.5)
        self.blocked_during_smtp = self.thread.is_alive()
        self.membership_during_smtp = _membership_count(self.db, RECIPIENT)
        return self.inner.send(message, sender, recipient)


def _membership_count(db: DbHarness, ref: str) -> int:
    tenant = db.foundation.tenants["niletech"]
    return int(
        owner_exec(
            db,
            "SELECT count(*) FROM group_memberships WHERE group_id = :g AND user_id = :u",
            {"g": tenant.groups["finance-team"].id, "u": user_id(db, ref)},
        ).scalar_one()
    )


def test_revocation_during_the_handoff_waits_until_the_outcome_is_recorded(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    host, port, api = mailpit
    mailer = RevokeDuringSmtp(SmtpMailer(host, port, 5), actions_db)
    client = make_client(mailer=mailer)
    delivery = app_state(client).email_delivery
    _, _draft, row = queued(client, actions_db, recipient)
    assert _membership_count(actions_db, RECIPIENT) == 1  # something to revoke
    try:
        assert delivery.deliver(row.execution_key, row.tenant_id) == "delivered"
        assert mailer.thread is not None
        mailer.thread.join(10)
        # While SMTP ran, the revocation had not committed (it waited on the lock)...
        assert mailer.membership_during_smtp == 1
        assert mailer.blocked_during_smtp
        # ...and it committed once the outcome was recorded.
        assert not mailer.thread.is_alive()
        assert _membership_count(actions_db, RECIPIENT) == 0
    finally:
        _membership(actions_db, RECIPIENT, present=True)
    assert len(mailpit_messages(api, row.message_id)) == 1


class CountingMailer:
    """The real SMTP mailer, counting every SMTP attempt the application makes."""

    def __init__(self, inner: SmtpMailer) -> None:
        self.inner, self.calls = inner, 0

    def send(self, message: EmailMessage, sender: str, recipient: str) -> Accepted:
        self.calls += 1
        return self.inner.send(message, sender, recipient)


def smtp_attempts(db: DbHarness, execution_key: Any) -> list[int]:
    return list(
        owner_exec(
            db,
            "SELECT attempt FROM email_smtp_attempts WHERE execution_key = :k ORDER BY attempt",
            {"k": execution_key},
        ).scalars()
    )


def test_receipt_deleted_after_acceptance_stays_uncertain_and_is_never_resent(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    """Mailpit accepts the email, the worker crashes before recording confirmation, and
    only that email is deleted from Mailpit (other messages, including older receipts,
    are untouched). Absence no longer proves the attempt never reached SMTP, so the
    delivery stays uncertain and no second SMTP attempt is made."""

    host, port, api = mailpit
    mailer = CountingMailer(SmtpMailer(host, port, 5))
    client = make_client(mailer=mailer)
    # An earlier, confirmed delivery whose receipt stays in Mailpit throughout.
    _, _, earlier = queued(client, actions_db, recipient)
    delivery = app_state(client).email_delivery
    assert delivery.deliver(earlier.execution_key, earlier.tenant_id) == "delivered"

    _, draft, row = queued(client, actions_db, recipient)
    delivery.hooks["after_accept"] = _crash
    try:
        with pytest.raises(SimulatedCrash):
            delivery.deliver(row.execution_key, row.tenant_id)
    finally:
        delivery.hooks.clear()
    assert mailer.calls == 2
    assert smtp_attempts(actions_db, row.execution_key) == [1]
    deadline = time.monotonic() + 10
    while not mailpit_messages(api, row.message_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    (accepted,) = mailpit_messages(api, row.message_id)
    assert outbox(actions_db, draft["action_id"]).confirmed_at is None

    # Delete only that email.
    deleted = httpx.request(
        "DELETE", f"{api}/api/v1/messages", json={"IDs": [accepted["ID"]]}, timeout=5
    )
    deleted.raise_for_status()
    assert mailpit_messages(api, row.message_id) == []
    assert len(mailpit_messages(api, earlier.message_id)) == 1

    time.sleep(1.2)  # lease expires
    worker = app_state(client).action_worker
    try:
        assert worker.tick()["stale"] == ["uncertain"]
        for _ in range(3):
            assert worker.tick()["reconcile"] == ["receipt_missing"]
        final = outbox(actions_db, draft["action_id"])
        assert final.status == "uncertain"
        assert final.status_reason == "receipt_missing_after_smtp"
        assert final.attempts == 1 and final.confirmed_at is None
        assert mailer.calls == 2  # no second SMTP attempt
        assert smtp_attempts(actions_db, row.execution_key) == [1]
        assert mailpit_messages(api, row.message_id) == []
        shown = client.get(
            f"/api/v1/actions/email-drafts/{draft['action_id']}",
            headers=auth(login(client, actions_db, FINANCE)),
        ).json()
        assert shown["status"] == "uncertain"
        assert shown["status_reason"] == "receipt_missing_after_smtp"
        trail = audit_reasons(actions_db, draft["action_id"])
        assert trail.count("email.deliver:info:receipt_missing_after_smtp") == 1
    finally:
        # Operator resolution so later tests' scheduler passes are not affected.
        owner_exec(
            actions_db,
            "UPDATE email_outbox SET status = 'failed', status_reason = 'test_cleanup'"
            " WHERE execution_key = :k",
            {"k": row.execution_key},
        )


def _restart_mailpit(api: str) -> None:
    command = os.environ.get("TEST_MAILPIT_RESTART_CMD")
    if not command:
        message = "TEST_MAILPIT_RESTART_CMD is required for the Mailpit restart test"
        if os.environ.get("EAIOS_REQUIRE_MAILPIT_RESTART") == "1":
            pytest.fail(message)
        pytest.skip(message)
    subprocess.run(shlex.split(command), check=True, capture_output=True, timeout=60)  # noqa: S603
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{api}/api/v1/info", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError("Mailpit did not come back")


def test_mailpit_restart_after_acceptance_keeps_the_receipt(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    """Mailpit accepts the message, the worker dies before recording it, Mailpit and the
    application both restart: the persisted receipt is found and nothing is re-sent."""

    client = make_client()
    _, draft, row = queued(client, actions_db, recipient)
    delivery = app_state(client).email_delivery
    delivery.hooks["after_accept"] = _crash
    with pytest.raises(SimulatedCrash):
        delivery.deliver(row.execution_key, row.tenant_id)
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1
    assert outbox(actions_db, draft["action_id"]).status == "dispatching"

    _restart_mailpit(mailpit[2])
    restarted = make_client()  # a new application process
    time.sleep(1.2)
    worker = app_state(restarted).action_worker
    assert worker.tick()["stale"] == ["uncertain"]
    assert worker.tick()["reconcile"] == ["delivered"]
    final = outbox(actions_db, draft["action_id"])
    assert final.status == "delivered" and final.status_reason == "reconciled_found"
    assert final.attempts == 1
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1


def test_cancel_waits_for_the_claim_instead_of_deadlocking(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    """Deterministic interleaving: the claim holds the action and outbox locks while a
    cancel starts. The cancel waits on the action lock (same order), then reports that
    it is too late; nothing deadlocks and exactly one email is sent."""

    client = make_client()
    token, draft, row = queued(client, actions_db, recipient)
    state = app_state(client)
    ctx = state.identity.authenticate(token, None)
    results: list[Any] = []

    def cancel() -> None:
        try:
            results.append(state.actions.cancel(ctx, uuid.UUID(draft["action_id"])))
        except Exception as exc:
            results.append(exc)

    canceller = threading.Thread(target=cancel)

    def during_claim(_: Any) -> None:
        canceller.start()
        canceller.join(0.5)
        assert canceller.is_alive()  # waiting for the action lock, not deadlocked

    state.email_delivery.hooks["claim_locked"] = during_claim
    try:
        outcome = state.email_delivery.deliver(row.execution_key, row.tenant_id)
    finally:
        state.email_delivery.hooks.clear()
    canceller.join(10)
    assert outcome == "delivered"
    (result,) = results
    assert getattr(result, "code", None) in ("action_locked", "too_late"), result
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1


def test_cancel_racing_dispatch_is_consistent(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    state = app_state(client)
    for _ in range(8):
        token, draft, row = queued(client, actions_db, recipient)
        ctx = state.identity.authenticate(token, None)
        barrier = threading.Barrier(2)
        out: dict[str, Any] = {}

        def run(
            name: str,
            fn: Callable[[], Any],
            out: dict[str, Any] = out,
            barrier: threading.Barrier = barrier,
        ) -> None:
            barrier.wait()
            try:
                out[name] = fn()
            except Exception as exc:
                out[name] = exc

        threads = [
            threading.Thread(
                target=run,
                args=(
                    "cancel",
                    lambda d=draft, c=ctx: state.actions.cancel(c, uuid.UUID(d["action_id"])),
                ),
            ),
            threading.Thread(
                target=run,
                args=(
                    "deliver",
                    lambda r=row: state.email_delivery.deliver(r.execution_key, r.tenant_id),
                ),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not isinstance(out["deliver"], Exception), out
        final = outbox(actions_db, draft["action_id"])
        sent = len(mailpit_messages(mailpit[2], row.message_id))
        if final.status == "cancelled":
            assert sent == 0 and out["deliver"] == "not_pending:cancelled"
            assert getattr(out["cancel"], "status", None) == "cancelled"
        else:
            assert final.status == "delivered" and sent == 1
            assert getattr(out["cancel"], "code", None) in ("action_locked", "too_late")


class AcceptThenTimeout:
    """Hands the message to the real Mailpit, then reports a timeout waiting for the
    reply: the classic ambiguous SMTP outcome."""

    def __init__(self, inner: SmtpMailer) -> None:
        self.inner = inner

    def send(self, message: EmailMessage, sender: str, recipient: str) -> Accepted:
        self.inner.send(message, sender, recipient)
        raise Ambiguous("TimeoutError after DATA")


class ClosedMailpit(MailpitLookup):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9", timeout=0.5)


def test_ambiguous_smtp_outcome_is_uncertain_and_never_blindly_retried(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    host, port, api = mailpit
    client = make_client(
        mailer=AcceptThenTimeout(SmtpMailer(host, port, 5)), lookup=ClosedMailpit()
    )
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    row = wait_outbox(actions_db, draft["action_id"], {"uncertain"})
    assert row.status_reason == "smtp_ambiguous" and row.uncertain_since is not None
    shown = client.get(f"/api/v1/actions/email-drafts/{draft['action_id']}", headers=auth(token))
    assert shown.json()["status"] == "uncertain"

    worker = app_state(client).action_worker
    # Mailpit's API is unreachable: the send stays uncertain; nothing is retried.
    for _ in range(3):
        assert worker.tick()["reconcile"] == ["unavailable"]
    assert outbox(actions_db, draft["action_id"]).status == "uncertain"
    assert len(mailpit_messages(api, row.message_id)) == 1

    worker.delivery.lookup = MailpitLookup(api)  # the API comes back
    assert worker.tick()["reconcile"] == ["delivered"]
    assert len(mailpit_messages(api, row.message_id)) == 1


def test_duplicate_delivery_attempts_are_no_ops(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    app_state(client).actions.dispatch = None
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    row = outbox(actions_db, draft["action_id"])
    delivery = app_state(client).email_delivery
    with ThreadPoolExecutor(4) as pool:
        outcomes = list(
            pool.map(lambda _: delivery.deliver(row.execution_key, row.tenant_id), range(4))
        )
    assert outcomes.count("delivered") == 1, outcomes
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1


def test_unreachable_smtp_before_data_is_retried_safely(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client(mailer=SmtpMailer("127.0.0.1", 9, 0.5))
    delivery = app_state(client).email_delivery
    token, draft, approval = flow_to_approval(client, actions_db, recipient)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    deadline = time.monotonic() + 5
    while outbox(actions_db, draft["action_id"]).attempts < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    row = wait_outbox(actions_db, draft["action_id"], {"pending"})
    assert row.status_reason == "not_sent_retry" and row.attempts == 1
    delivery.mailer = SmtpMailer(mailpit[0], mailpit[1], 5)
    assert app_state(client).action_worker.tick()["deliver"] == ["delivered"]
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1


# -- scheduled briefings -----------------------------------------------------------------


def create_schedule(client: TestClient, token: str, to: str, question: str = Q1) -> dict[str, Any]:
    r = client.post(
        "/api/v1/actions/briefings",
        json={
            "question": question,
            "recipient_user_id": to,
            "interval_seconds": 86400,
            "first_run_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def occurrences(db: DbHarness, schedule_id: str) -> list[Any]:
    return list(
        owner_exec(
            db,
            "SELECT * FROM briefing_occurrences WHERE schedule_id = :s ORDER BY due_at",
            {"s": schedule_id},
        ).all()
    )


def run_schedule(client: TestClient, db: DbHarness, schedule_id: str) -> Any:
    worker = app_state(client).action_worker
    worker.tick()
    (occ,) = occurrences(db, schedule_id)
    if occ.run_id is not None:
        wait_for_run(db, str(occ.run_id))
        worker.tick()
    (occ,) = occurrences(db, schedule_id)
    return occ


def test_scheduled_briefing_produces_a_draft_and_never_sends_without_approval(
    make_client: Callable[..., TestClient],
    actions_db: DbHarness,
    recipient: str,
    mailpit: tuple[str, int, str],
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    schedule = create_schedule(client, token, recipient)
    occ = run_schedule(client, actions_db, schedule["schedule_id"])
    assert occ.status == "review" and occ.status_reason == "awaiting_human_approval"
    assert occ.policy_epoch is not None and occ.action_id is not None
    action = str(occ.action_id)

    # Many more scheduler passes: still a draft, no approval, nothing in the outbox.
    for _ in range(3):
        app_state(client).action_worker.tick()
    assert (
        owner_exec(
            actions_db, "SELECT count(*) FROM email_approvals WHERE action_id = :a", {"a": action}
        ).scalar_one()
        == 0
    )
    assert outbox(actions_db, action) is None
    draft = client.get(f"/api/v1/actions/email-drafts/{action}", headers=auth(token)).json()
    assert draft["origin"] == "briefing" and draft["status"] == "draft"
    assert Q1_NET in draft["body"]
    # The next occurrence is scheduled, once.
    listed = client.get("/api/v1/actions/briefings", headers=auth(token)).json()
    (mine,) = [s for s in listed if s["schedule_id"] == schedule["schedule_id"]]
    assert datetime.fromisoformat(mine["next_run_at"]) > datetime.now(UTC)
    assert len(mine["occurrences"]) == 1

    # Sending needs an approval like any other draft.
    r = send(client, token, draft, secrets.token_urlsafe(32))
    assert r.status_code == 409
    approval = approve(client, token, draft)
    assert send(client, token, draft, approval["approval_token"]).status_code == 202
    row = wait_outbox(actions_db, action, {"delivered"})
    assert len(mailpit_messages(mailpit[2], row.message_id)) == 1


def test_scheduled_briefing_reloads_permissions_at_run_time(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    schedule = create_schedule(client, token, recipient)
    # Access removed after the schedule was created: the run uses current permissions.
    with without_finance_team(actions_db, FINANCE):
        occ = run_schedule(client, actions_db, schedule["schedule_id"])
    assert occ.status == "failed" and occ.action_id is None
    run = owner_exec(
        actions_db, "SELECT status, result FROM report_runs WHERE id = :r", {"r": occ.run_id}
    ).one()
    assert run.status != "published" and run.result is None


def test_scheduled_briefing_for_deactivated_owner_is_blocked(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    schedule = create_schedule(client, token, recipient)
    owner = user_id(actions_db, FINANCE)
    owner_exec(actions_db, "UPDATE users SET status = 'inactive' WHERE id = :u", {"u": owner})
    try:
        app_state(client).action_worker.tick()
    finally:
        owner_exec(actions_db, "UPDATE users SET status = 'active' WHERE id = :u", {"u": owner})
    (occ,) = occurrences(actions_db, schedule["schedule_id"])
    assert occ.status == "blocked" and occ.status_reason == "owner_inactive"
    assert occ.run_id is None and occ.action_id is None


def test_scheduled_briefing_recipient_without_access_is_blocked(
    make_client: Callable[..., TestClient], actions_db: DbHarness
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    board = user_id(actions_db, "niletech/board-member")
    schedule = create_schedule(client, token, board)
    occ = run_schedule(client, actions_db, schedule["schedule_id"])
    assert occ.status == "blocked" and occ.status_reason == "recipient_not_authorized"
    assert occ.action_id is None


def test_briefings_accept_report_questions_only(
    make_client: Callable[..., TestClient], actions_db: DbHarness, recipient: str
) -> None:
    client = make_client()
    token = login(client, actions_db, FINANCE)
    r = client.post(
        "/api/v1/actions/briefings",
        json={
            "question": "What does the leave policy say about notice?",
            "recipient_user_id": recipient,
            "interval_seconds": 86400,
            "first_run_at": datetime.now(UTC).isoformat(),
        },
        headers=auth(token),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_question"


def test_migration_0012_upgrades_and_downgrades_cleanly() -> None:
    """From M5 (0011) with seeded data to head and back: M6 tables appear and go, and
    tenant settings keep their M5 values on the way down."""

    from alembic import command
    from alembic.config import Config

    from tests.integration.conftest import migrate

    with disposable_database(seed=True, revision="0011_m5_wave2") as db:
        migrate(db, "head")
        tables = owner_exec(
            db, "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'email_%'"
        ).scalar_one()
        assert tables == 5  # actions, versions, approvals, outbox, smtp_attempts
        owner_exec(
            db,
            "UPDATE tenant_settings SET enabled_packs = enabled_packs || ARRAY['actions'],"
            " enabled_connectors = enabled_connectors || ARRAY['mailpit']",
        )
        config = Config("alembic.ini")
        config.attributes["url"] = db.owner_url
        # Downgrades grant to this database's own app role, as upgrades do.
        with pytest.MonkeyPatch.context() as env:
            env.setenv("EAIOS_APP_DB_ROLE", db.app_role)
            env.setenv("EAIOS_REPORT_DB_ROLE", db.report_role)
            command.downgrade(config, "0011_m5_wave2")
        assert (
            owner_exec(
                db, "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'email_%'"
            ).scalar_one()
            == 0
        )
        packs = owner_exec(db, "SELECT enabled_packs FROM tenant_settings").scalars().all()
        assert all("actions" not in p for p in packs)
        migrate(db, "head")
