"""Document authorization: eligible point ids, search filters and access rechecks.

Implements ADR 0104 (now accepted, ADR 0303): every search is constrained to the
point ids of documents the caller may read **now**, in their active generation.

Eligibility is computed by the reference evaluator itself (``security.policy``) over
resource metadata and grants loaded from PostgreSQL, so the search filter and the
policy cannot drift apart: there is no second, compiled implementation of the five
layers to keep equivalent. This scales to the project's small corpus (hundreds to low
thousands of points); a compiled payload filter would need its own equivalence test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.documents import store
from app.repositories.identity import load_subject
from app.repositories.resources import load_resource_meta, load_resource_metas
from app.security.policy import Action, Decision, Reason, ResourceMeta, Subject, evaluate


@dataclass(frozen=True, slots=True)
class Eligibility:
    policy_epoch: int
    document_ids: tuple[UUID, ...]
    point_ids: tuple[UUID, ...]
    denied: dict[UUID, str]  # private reasons, for the audit log only


def eligible(conn: Connection, user_id: UUID) -> Eligibility:
    epoch = int(conn.execute(text("SELECT policy_epoch FROM tenants")).scalar_one())
    subject = load_subject(conn, user_id)
    docs = store.searchable_documents(conn)
    metas = load_resource_metas(conn, [d.id for d in docs])
    allowed: list[UUID] = []
    denied: dict[UUID, str] = {}
    for doc in docs:
        meta = metas.get(doc.id)
        decision = (
            evaluate(subject, Action.RESOURCE_READ.value, meta)
            if subject is not None and meta is not None
            else Decision.deny(Reason.NOT_FOUND)
        )
        if decision.allowed:
            allowed.append(doc.id)
        else:
            denied[doc.id] = decision.reason.value
    return Eligibility(
        policy_epoch=epoch,
        document_ids=tuple(allowed),
        point_ids=tuple(store.active_point_ids(conn, allowed)),
        denied=denied,
    )


def search_filter(tenant_id: UUID, point_ids: tuple[UUID, ...]) -> dict[str, Any]:
    """The filter attached to every Qdrant search branch (T9)."""

    return {
        "must": [
            {"key": "tenant_id", "match": {"value": str(tenant_id)}},
            {"has_id": [str(p) for p in point_ids]},
        ]
    }


def can_read(conn: Connection, user_id: UUID, document_id: UUID) -> Decision:
    subject = load_subject(conn, user_id)
    meta = load_resource_meta(conn, document_id)
    if subject is None or meta is None:
        return Decision.deny(Reason.NOT_FOUND)
    return evaluate(subject, Action.RESOURCE_READ.value, meta)


def can_administer(subject: Subject, tenant_id: UUID, department: str) -> Decision:
    """Uploading and setting permissions require ``resource.admin`` covering the
    document's department (metadata administration; never implies reading)."""

    proposed = ResourceMeta(
        tenant_id=tenant_id,
        resource_id=tenant_id,
        classification="restricted",
        department=department,
        department_restricted=True,
        allowed_countries=("*",),
    )
    return evaluate(subject, Action.RESOURCE_ADMIN.value, proposed)


def can_administer_document(conn: Connection, user_id: UUID, document_id: UUID) -> Decision:
    subject = load_subject(conn, user_id)
    meta = load_resource_meta(conn, document_id)
    if subject is None or meta is None:
        return Decision.deny(Reason.NOT_FOUND)
    return evaluate(subject, Action.RESOURCE_ADMIN.value, meta)
