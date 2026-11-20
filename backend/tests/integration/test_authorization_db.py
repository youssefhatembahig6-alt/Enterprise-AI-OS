"""Authorization from current PostgreSQL state: fixture truth table, live changes, epoch."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.db.engine import tenant_transaction
from app.fixtures import load_policy_cases
from app.security.context import AccessContext
from app.security.policy import Reason
from app.services.authorization import authorize_resource
from app.services.identity import IdentityService
from tests.integration.conftest import DbHarness, login, owner_exec

CASES = load_policy_cases()
AdminTx = Callable[[str, dict[str, object]], None]
Ids = dict[str, Any]


def identity(client: TestClient) -> IdentityService:
    service: IdentityService = client.app.state.identity  # type: ignore[attr-defined]
    return service


def context_for(client: TestClient, token: str) -> AccessContext:
    return identity(client).authenticate(token, request_id="test")


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['user']}->{c['resource']}:{c['action']}")
def test_database_decisions_match_reference_truth_table(
    client: TestClient, test_db: DbHarness, app_engine: Engine, case: dict[str, str]
) -> None:
    """Contexts and resources loaded through the app role agree with the in-memory table.

    Inactive users cannot log in, so their case is checked by the login tests and by
    the in-memory table; here we assert that authentication itself refuses them.
    """

    slug, key = case["user"].split("/", 1)
    user = test_db.foundation.tenants[slug].users[key]
    if user.status != "active":
        response = client.post(
            "/api/v1/auth/login",
            json={"tenant": slug, "email": user.email, "password": test_db.password(case["user"])},
        )
        assert response.status_code == 401
        return
    ctx = context_for(client, login(client, test_db, case["user"]))
    _, res = test_db.foundation.tenant_resource(case["resource"])
    decision = authorize_resource(app_engine, ctx, case["action"], res.id)
    expected_allow = case["expect"] == "allow"
    assert decision.allowed is expected_allow
    if case["reason"] == Reason.TENANT_MISMATCH.value:
        # Under RLS the other tenant's resource is simply invisible.
        assert decision.reason is Reason.NOT_FOUND
    else:
        assert decision.reason.value == case["reason"]


def epoch(test_db: DbHarness, slug: str) -> int:
    return int(
        owner_exec(
            test_db, "SELECT policy_epoch FROM tenants WHERE slug = :s", {"s": slug}
        ).scalar_one()
    )


@pytest.fixture
def niletech_admin_tx(app_engine: Engine, test_db: DbHarness) -> AdminTx:
    tenant_id = test_db.foundation.tenants["niletech"].id

    def run(sql: str, params: dict[str, object]) -> None:
        with tenant_transaction(app_engine, tenant_id) as conn:
            conn.execute(text(sql), params)

    return run


@pytest.fixture
def ids(test_db: DbHarness) -> Iterator[Ids]:
    t = test_db.foundation.tenants["niletech"]
    yield {
        "tenant": t.id,
        "reader": t.users["finance-reader"].id,
        "finance_team": t.groups["finance-team"].id,
        "reader_role": t.roles["reader"].id,
        "report": t.resources["finance-report"].id,
    }


def _assert_change(
    client: TestClient,
    test_db: DbHarness,
    app_engine: Engine,
    report: Any,
    change: Callable[[], None],
    restore: Callable[[], None],
    expected_reason: Reason,
) -> None:
    token = login(client, test_db, "niletech/finance-reader")
    before_epoch = epoch(test_db, "niletech")
    assert authorize_resource(
        app_engine, context_for(client, token), "resource.read", report
    ).allowed
    change()
    try:
        after_epoch = epoch(test_db, "niletech")
        assert after_epoch > before_epoch, "policy epoch must advance"
        ctx = context_for(client, token)  # same, unexpired JWT
        assert ctx.policy_epoch == after_epoch
        decision = authorize_resource(app_engine, ctx, "resource.read", report)
        assert not decision.allowed
        assert decision.reason is expected_reason
    finally:
        restore()
    assert epoch(test_db, "niletech") > after_epoch
    assert authorize_resource(
        app_engine, context_for(client, token), "resource.read", report
    ).allowed


def test_group_removal_changes_next_decision(
    client: TestClient, test_db: DbHarness, app_engine: Engine, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx(
            "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u",
            {"g": ids["finance_team"], "u": ids["reader"]},
        ),
        lambda: niletech_admin_tx(
            "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)",
            {"t": ids["tenant"], "g": ids["finance_team"], "u": ids["reader"]},
        ),
        Reason.NO_GRANT,
    )


def test_role_removal_changes_next_decision(
    client: TestClient, test_db: DbHarness, app_engine: Engine, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx(
            "DELETE FROM user_roles WHERE user_id = :u AND role_id = :r",
            {"u": ids["reader"], "r": ids["reader_role"]},
        ),
        lambda: niletech_admin_tx(
            "INSERT INTO user_roles (tenant_id, user_id, role_id) VALUES (:t, :u, :r)",
            {"t": ids["tenant"], "u": ids["reader"], "r": ids["reader_role"]},
        ),
        Reason.MISSING_CAPABILITY,
    )


@pytest.mark.parametrize(
    ("column", "value", "original", "reason"),
    [
        ("department", "sales", "finance", Reason.DEPARTMENT_ATTRIBUTE),
        ("country", "AE", "EG", Reason.COUNTRY_ATTRIBUTE),
        ("clearance", "internal", "confidential", Reason.CLEARANCE),
    ],
)
def test_attribute_change_changes_next_decision(
    client: TestClient,
    test_db: DbHarness,
    app_engine: Engine,
    niletech_admin_tx: AdminTx,
    ids: Ids,
    column: str,
    value: str,
    original: str,
    reason: Reason,
) -> None:
    sql = f"UPDATE users SET {column} = :v WHERE id = :u"  # noqa: S608 - fixed column list
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx(sql, {"v": value, "u": ids["reader"]}),
        lambda: niletech_admin_tx(sql, {"v": original, "u": ids["reader"]}),
        reason,
    )


def test_explicit_deny_added_changes_next_decision(
    client: TestClient, test_db: DbHarness, app_engine: Engine, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx(
            "INSERT INTO resource_grants (tenant_id, resource_id, user_id, effect, action)"
            " VALUES (:t, :r, :u, 'deny', 'resource.read')",
            {"t": ids["tenant"], "r": ids["report"], "u": ids["reader"]},
        ),
        lambda: niletech_admin_tx(
            "DELETE FROM resource_grants WHERE resource_id = :r AND user_id = :u AND effect = 'deny'",
            {"r": ids["report"], "u": ids["reader"]},
        ),
        Reason.EXPLICIT_DENY,
    )


def test_grant_removal_changes_next_decision(
    client: TestClient, test_db: DbHarness, app_engine: Engine, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    grant_id = test_db.foundation.tenants["niletech"].resources["finance-report"].grants[0].id
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx("DELETE FROM resource_grants WHERE id = :id", {"id": grant_id}),
        lambda: niletech_admin_tx(
            "INSERT INTO resource_grants (id, tenant_id, resource_id, group_id, effect, action)"
            " VALUES (:id, :t, :r, :g, 'allow', 'resource.read')",
            {"id": grant_id, "t": ids["tenant"], "r": ids["report"], "g": ids["finance_team"]},
        ),
        Reason.NO_GRANT,
    )


def test_classification_change_changes_next_decision(
    client: TestClient, test_db: DbHarness, app_engine: Engine, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    sql = "UPDATE resources SET classification = :c WHERE id = :r"
    _assert_change(
        client,
        test_db,
        app_engine,
        ids["report"],
        lambda: niletech_admin_tx(sql, {"c": "restricted", "r": ids["report"]}),
        lambda: niletech_admin_tx(sql, {"c": "confidential", "r": ids["report"]}),
        Reason.CLEARANCE,
    )


def test_policy_change_in_one_tenant_does_not_bump_the_other(
    test_db: DbHarness, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    other_before = epoch(test_db, "isolation-test")
    niletech_admin_tx("UPDATE users SET country = 'EG' WHERE id = :u", {"u": ids["reader"]})
    assert epoch(test_db, "isolation-test") == other_before


def test_unrelated_update_does_not_bump_epoch(
    test_db: DbHarness, niletech_admin_tx: AdminTx, ids: Ids
) -> None:
    before = epoch(test_db, "niletech")
    niletech_admin_tx(
        "UPDATE users SET display_name = display_name WHERE id = :u", {"u": ids["reader"]}
    )
    assert epoch(test_db, "niletech") == before


def test_authorization_decisions_are_audited(
    client: TestClient, test_db: DbHarness, app_engine: Engine
) -> None:
    ctx = context_for(client, login(client, test_db, "niletech/denied-reader"))
    res = test_db.foundation.tenants["niletech"].resources["finance-report"]
    authorize_resource(app_engine, ctx, "resource.read", res.id)
    row = owner_exec(
        test_db,
        "SELECT decision, reason, resource_id, tenant_id, details FROM audit_events"
        " WHERE event_type = 'authz.decision' AND user_id = :u ORDER BY occurred_at DESC LIMIT 1",
        {"u": ctx.user_id},
    ).one()
    assert (row.decision, row.reason, row.resource_id, row.tenant_id) == (
        "deny",
        "deny.explicit_deny",
        res.id,
        ctx.tenant_id,
    )
    assert row.details["policy_epoch"] == ctx.policy_epoch
    assert "NILETECH-FINANCE-SECRET-7421" not in str(tuple(row))
