"""Resource metadata and grants (metadata only; no content in M0)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, text

from app.security.policy import Grant, ResourceMeta


def load_resource_meta(conn: Connection, resource_id: UUID) -> ResourceMeta | None:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, classification, department, department_restricted,
                   allowed_countries, status
              FROM resources WHERE id = :rid
            """
        ),
        {"rid": resource_id},
    ).one_or_none()
    if row is None:
        return None
    grants = conn.execute(
        text(
            "SELECT effect, action, user_id, group_id FROM resource_grants"
            " WHERE tenant_id = :tid AND resource_id = :rid"
        ),
        {"tid": row.tenant_id, "rid": resource_id},
    ).all()
    return ResourceMeta(
        tenant_id=row.tenant_id,
        resource_id=row.id,
        classification=row.classification,
        department=row.department,
        department_restricted=row.department_restricted,
        allowed_countries=tuple(row.allowed_countries),
        active=row.status == "active",
        grants=tuple(
            Grant(effect=g.effect, action=g.action, user_id=g.user_id, group_id=g.group_id)
            for g in grants
        ),
    )
