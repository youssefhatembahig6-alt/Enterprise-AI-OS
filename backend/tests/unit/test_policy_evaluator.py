"""Reference evaluator: each layer fails independently; deny wins; unknowns deny."""

from __future__ import annotations

import dataclasses
import itertools
import uuid

import pytest

from app.security.policy import (
    CLASSIFICATIONS,
    Action,
    CapabilityGrant,
    Decision,
    Grant,
    Reason,
    ResourceMeta,
    Subject,
    evaluate,
)

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
USER = uuid.uuid4()
OTHER_USER = uuid.uuid4()
GROUP = uuid.uuid4()
OTHER_GROUP = uuid.uuid4()
READ = Action.RESOURCE_READ.value
ADMIN = Action.RESOURCE_ADMIN.value


def subject(**overrides: object) -> Subject:
    base = Subject(
        tenant_id=TENANT_A,
        user_id=USER,
        active=True,
        capabilities=frozenset({CapabilityGrant("resource.read")}),
        group_ids=frozenset({GROUP}),
        clearance="confidential",
        country="EG",
        department="finance",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def resource(**overrides: object) -> ResourceMeta:
    base = ResourceMeta(
        tenant_id=TENANT_A,
        resource_id=uuid.uuid4(),
        classification="confidential",
        department="finance",
        department_restricted=True,
        allowed_countries=("EG",),
        grants=(Grant(effect="allow", action=READ, group_id=GROUP),),
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def test_all_five_layers_pass_allows() -> None:
    assert evaluate(subject(), READ, resource()) == Decision.allow()


@pytest.mark.parametrize(
    ("subject_overrides", "resource_overrides", "reason"),
    [
        # Layer 1: tenant
        ({}, {"tenant_id": TENANT_B}, Reason.TENANT_MISMATCH),
        # Layer 2: role capability
        ({"capabilities": frozenset()}, {}, Reason.MISSING_CAPABILITY),
        (
            {"capabilities": frozenset({CapabilityGrant("resource.admin")})},
            {},
            Reason.MISSING_CAPABILITY,
        ),
        (
            {"capabilities": frozenset({CapabilityGrant("resource.read", "hr")})},
            {},
            Reason.CAPABILITY_OUT_OF_SCOPE,
        ),
        # Layer 3: attributes
        ({"department": "sales"}, {}, Reason.DEPARTMENT_ATTRIBUTE),
        ({"country": "AE"}, {}, Reason.COUNTRY_ATTRIBUTE),
        # Layer 4: grant
        ({"group_ids": frozenset({OTHER_GROUP})}, {}, Reason.NO_GRANT),
        ({}, {"grants": ()}, Reason.NO_GRANT),
        # Layer 5: classification
        ({"clearance": "internal"}, {}, Reason.CLEARANCE),
        ({}, {"classification": "restricted"}, Reason.CLEARANCE),
    ],
)
def test_each_layer_failure_denies_independently(
    subject_overrides: dict[str, object], resource_overrides: dict[str, object], reason: Reason
) -> None:
    decision = evaluate(subject(**subject_overrides), READ, resource(**resource_overrides))
    assert decision == Decision.deny(reason)


def test_inactive_subject_denied() -> None:
    assert evaluate(subject(active=False), READ, resource()).reason is Reason.INACTIVE_SUBJECT


def test_deleted_resource_denied() -> None:
    assert evaluate(subject(), READ, resource(active=False)).reason is Reason.RESOURCE_INACTIVE


def test_direct_user_grant_satisfies_grant_layer() -> None:
    res = resource(grants=(Grant(effect="allow", action=READ, user_id=USER),))
    assert evaluate(subject(group_ids=frozenset()), READ, res).allowed


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"capabilities": frozenset()}, Reason.MISSING_CAPABILITY),
        ({"department": "sales"}, Reason.DEPARTMENT_ATTRIBUTE),
        ({"country": "AE"}, Reason.COUNTRY_ATTRIBUTE),
        ({"clearance": "internal"}, Reason.CLEARANCE),
        ({"tenant_id": TENANT_B}, Reason.TENANT_MISMATCH),
    ],
)
def test_direct_user_grant_satisfies_only_the_grant_layer(
    overrides: dict[str, object], reason: Reason
) -> None:
    res = resource(grants=(Grant(effect="allow", action=READ, user_id=USER),))
    assert evaluate(subject(group_ids=frozenset(), **overrides), READ, res).reason is reason


def test_grant_for_another_user_does_not_apply() -> None:
    res = resource(grants=(Grant(effect="allow", action=READ, user_id=OTHER_USER),))
    assert evaluate(subject(group_ids=frozenset()), READ, res).reason is Reason.NO_GRANT


@pytest.mark.parametrize(
    "deny",
    [
        Grant(effect="deny", action=READ, user_id=USER),
        Grant(effect="deny", action=READ, group_id=GROUP),
    ],
)
def test_explicit_deny_overrides_allow(deny: Grant) -> None:
    res = resource(
        grants=(
            Grant(effect="allow", action=READ, user_id=USER),
            Grant(effect="allow", action=READ, group_id=GROUP),
            deny,
        )
    )
    assert evaluate(subject(), READ, res) == Decision.deny(Reason.EXPLICIT_DENY)


def test_deny_for_unrelated_principal_is_ignored() -> None:
    res = resource(
        grants=(
            Grant(effect="allow", action=READ, group_id=GROUP),
            Grant(effect="deny", action=READ, user_id=OTHER_USER),
        )
    )
    assert evaluate(subject(), READ, res).allowed


@pytest.mark.parametrize(
    ("subject_overrides", "resource_overrides"),
    [
        ({"clearance": None}, {}),
        ({"department": None}, {}),
        ({"country": None}, {}),
        ({}, {"classification": None}),
        ({}, {"department_restricted": None}),
        ({}, {"allowed_countries": None}),
        ({}, {"department": None}),
        ({}, {"classification": "top-secret"}),
        ({"clearance": "ultra"}, {}),
        ({}, {"grants": (Grant(effect="allow", action=READ),)}),
        ({}, {"grants": (Grant(effect="maybe", action=READ, group_id=GROUP),)}),
    ],
)
def test_unknown_required_inputs_deny(
    subject_overrides: dict[str, object], resource_overrides: dict[str, object]
) -> None:
    decision = evaluate(subject(**subject_overrides), READ, resource(**resource_overrides))
    assert not decision.allowed
    assert decision.reason is Reason.UNKNOWN_INPUT


def test_unknown_action_denies() -> None:
    assert evaluate(subject(), "resource.delete", resource()).reason is Reason.UNKNOWN_ACTION


def test_unknown_attributes_allowed_when_resource_is_unrestricted() -> None:
    res = resource(department_restricted=False, allowed_countries=("*",))
    assert evaluate(subject(department=None, country=None), READ, res).allowed


def test_empty_country_list_denies() -> None:
    assert evaluate(subject(), READ, resource(allowed_countries=())).reason is (
        Reason.COUNTRY_ATTRIBUTE
    )


@pytest.mark.parametrize(
    ("clearance", "classification"), list(itertools.product(CLASSIFICATIONS, CLASSIFICATIONS))
)
def test_classification_matrix(clearance: str, classification: str) -> None:
    decision = evaluate(subject(clearance=clearance), READ, resource(classification=classification))
    expected = CLASSIFICATIONS.index(clearance) >= CLASSIFICATIONS.index(classification)
    assert decision.allowed is expected
    if not expected:
        assert decision.reason is Reason.CLEARANCE


def test_public_classification_does_not_cross_tenants() -> None:
    res = resource(
        tenant_id=TENANT_B,
        classification="public",
        department_restricted=False,
        allowed_countries=("*",),
    )
    assert evaluate(subject(clearance="restricted"), READ, res).reason is Reason.TENANT_MISMATCH


def test_public_classification_still_requires_capability_and_grant() -> None:
    res = resource(
        classification="public", department_restricted=False, allowed_countries=("*",), grants=()
    )
    assert evaluate(subject(), READ, res).reason is Reason.NO_GRANT
    assert evaluate(subject(capabilities=frozenset()), READ, res).reason is (
        Reason.MISSING_CAPABILITY
    )


def scoped_admin(scope: str | None) -> Subject:
    return subject(
        capabilities=frozenset({CapabilityGrant("resource.admin", scope)}),
        group_ids=frozenset(),
        clearance="public",
    )


def test_scoped_admin_manages_own_department() -> None:
    assert evaluate(scoped_admin("finance"), ADMIN, resource()).allowed


def test_scoped_admin_denied_outside_scope() -> None:
    decision = evaluate(scoped_admin("finance"), ADMIN, resource(department="hr"))
    assert decision.reason is Reason.CAPABILITY_OUT_OF_SCOPE


def test_tenant_wide_admin_manages_any_department() -> None:
    assert evaluate(scoped_admin(None), ADMIN, resource(department="hr")).allowed


def test_admin_rights_do_not_confer_content_access() -> None:
    res = resource(grants=(Grant(effect="allow", action=READ, user_id=USER),))
    decision = evaluate(scoped_admin(None), READ, res)
    assert decision.reason is Reason.MISSING_CAPABILITY


def test_admin_never_crosses_tenants() -> None:
    decision = evaluate(scoped_admin(None), ADMIN, resource(tenant_id=TENANT_B))
    assert decision.reason is Reason.TENANT_MISMATCH


def test_inactive_admin_denied() -> None:
    admin = dataclasses.replace(scoped_admin(None), active=False)
    assert evaluate(admin, ADMIN, resource()).reason is Reason.INACTIVE_SUBJECT
