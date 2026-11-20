"""The declarative fixture truth table evaluated by the reference policy (in memory)."""

from __future__ import annotations

import pytest

from app.fixtures import load_foundation, load_policy_cases, resource_for, subject_for
from app.security.policy import CLASSIFICATIONS, Capability, evaluate

FOUNDATION = load_foundation()
CASES = load_policy_cases()


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['user']}->{c['resource']}:{c['action']}")
def test_fixture_case(case: dict[str, str]) -> None:
    tenant_u, user = FOUNDATION.tenant_user(case["user"])
    tenant_r, res = FOUNDATION.tenant_resource(case["resource"])
    decision = evaluate(subject_for(tenant_u, user), case["action"], resource_for(tenant_r, res))
    assert decision.allowed is (case["expect"] == "allow")
    assert decision.reason.value == case["reason"]


def test_fixture_coverage_requirements() -> None:
    """Assignment item 10: each tenant has the required personas and policy features."""

    assert len(FOUNDATION.tenants) >= 2
    for tenant in FOUNDATION.tenants.values():
        users = tenant.users.values()
        assert any(u.status == "inactive" for u in users)
        assert any(
            ur.scope_department is not None
            for u in users
            for ur in u.roles
            if Capability.RESOURCE_ADMIN.value in tenant.roles[ur.role].capabilities
        ), "scoped admin"
        grants = [g for r in tenant.resources.values() for g in r.grants]
        assert any(g.user and g.effect == "allow" for g in grants), "direct user grant"
        assert any(g.group and g.effect == "allow" for g in grants), "group grant"
        assert any(g.effect == "deny" for g in grants), "explicit deny"
        assert {r.classification for r in tenant.resources.values()} == set(CLASSIFICATIONS)
        assert any(r.department_restricted for r in tenant.resources.values())
        assert any("*" not in r.allowed_countries for r in tenant.resources.values())


def test_cases_cover_both_tenants_and_both_actions() -> None:
    assert {c["user"].split("/")[0] for c in CASES} == set(FOUNDATION.tenants)
    assert {c["action"] for c in CASES} == {"resource.read", "resource.admin"}
    assert {c["expect"] for c in CASES} == {"allow", "deny"}
