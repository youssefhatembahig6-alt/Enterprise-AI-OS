"""Identity queries. All except ``lookup_login`` run under a tenant context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, text

from app.security.policy import CapabilityGrant, Subject


@dataclass(frozen=True, slots=True)
class LoginRecord:
    tenant_id: UUID
    tenant_active: bool
    user_id: UUID
    user_active: bool
    password_hash: str


def lookup_login(conn: Connection, tenant_slug: str, email: str) -> LoginRecord | None:
    """Restricted pre-authentication lookup through the SECURITY DEFINER function."""

    row = conn.execute(
        text("SELECT * FROM auth_lookup_login(:slug, :email)"),
        {"slug": tenant_slug, "email": email.lower()},
    ).one_or_none()
    if row is None:
        return None
    return LoginRecord(
        tenant_id=row.tenant_id,
        tenant_active=row.tenant_status == "active",
        user_id=row.user_id,
        user_active=row.user_status == "active",
        password_hash=row.password_hash,
    )


def create_session(
    conn: Connection, *, session_id: UUID, tenant_id: UUID, user_id: UUID, expires_at: datetime
) -> None:
    conn.execute(
        text(
            "INSERT INTO sessions (id, tenant_id, user_id, issued_at, expires_at)"
            " VALUES (:id, :tenant_id, :user_id, now(), :expires_at)"
        ),
        {"id": session_id, "tenant_id": tenant_id, "user_id": user_id, "expires_at": expires_at},
    )


def revoke_session(conn: Connection, session_id: UUID) -> bool:
    result = conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id},
    )
    return result.rowcount == 1


@dataclass(frozen=True, slots=True)
class SessionRow:
    session_id: UUID
    expires_at: datetime
    revoked: bool
    expired: bool
    user_id: UUID
    email: str
    display_name: str
    user_active: bool
    clearance: str | None
    country: str | None
    department: str | None
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str
    tenant_active: bool
    policy_epoch: int


def load_session(conn: Connection, session_id: UUID, user_id: UUID) -> SessionRow | None:
    row = conn.execute(
        text(
            """
            SELECT s.id AS session_id, s.expires_at, s.revoked_at IS NOT NULL AS revoked,
                   s.expires_at <= now() AS expired,
                   u.id AS user_id, u.email, u.display_name, u.status AS user_status,
                   u.clearance, u.country, u.department,
                   t.id AS tenant_id, t.slug, t.name, t.status AS tenant_status, t.policy_epoch
              FROM sessions s
              JOIN users u ON u.tenant_id = s.tenant_id AND u.id = s.user_id
              JOIN tenants t ON t.id = s.tenant_id
             WHERE s.id = :sid AND s.user_id = :uid
            """
        ),
        {"sid": session_id, "uid": user_id},
    ).one_or_none()
    if row is None:
        return None
    return SessionRow(
        session_id=row.session_id,
        expires_at=row.expires_at,
        revoked=row.revoked,
        expired=row.expired,
        user_id=row.user_id,
        email=row.email,
        display_name=row.display_name,
        user_active=row.user_status == "active",
        clearance=row.clearance,
        country=row.country,
        department=row.department,
        tenant_id=row.tenant_id,
        tenant_slug=row.slug,
        tenant_name=row.name,
        tenant_active=row.tenant_status == "active",
        policy_epoch=row.policy_epoch,
    )


def load_role_capabilities(
    conn: Connection, user_id: UUID
) -> list[tuple[str, str | None, str | None]]:
    """Return (role_key, capability or None, scope_department) for the user."""

    rows = conn.execute(
        text(
            """
            SELECT r.key, rc.capability, ur.scope_department
              FROM user_roles ur
              JOIN roles r ON r.tenant_id = ur.tenant_id AND r.id = ur.role_id
              LEFT JOIN role_capabilities rc ON rc.tenant_id = r.tenant_id AND rc.role_id = r.id
             WHERE ur.user_id = :uid
             ORDER BY r.key, rc.capability
            """
        ),
        {"uid": user_id},
    ).all()
    return [(r.key, r.capability, r.scope_department) for r in rows]


def load_groups(conn: Connection, user_id: UUID) -> list[tuple[UUID, str]]:
    rows = conn.execute(
        text(
            """
            SELECT g.id, g.key
              FROM group_memberships gm
              JOIN groups g ON g.tenant_id = gm.tenant_id AND g.id = gm.group_id
             WHERE gm.user_id = :uid
             ORDER BY g.key
            """
        ),
        {"uid": user_id},
    ).all()
    return [(r.id, r.key) for r in rows]


def load_subject(conn: Connection, user_id: UUID) -> Subject | None:
    """Current policy subject for a user, re-read from PostgreSQL (tenant context).

    Background work (report runs) uses this at every authorization point instead of a
    request-time snapshot, so deactivation, role, group and attribute changes apply.
    """

    row = conn.execute(
        text(
            """
            SELECT u.id, u.tenant_id, u.status, u.clearance, u.country, u.department,
                   t.status AS tenant_status
              FROM users u JOIN tenants t ON t.id = u.tenant_id
             WHERE u.id = :uid
            """
        ),
        {"uid": user_id},
    ).one_or_none()
    if row is None:
        return None
    caps = load_role_capabilities(conn, user_id)
    groups = load_groups(conn, user_id)
    return Subject(
        tenant_id=row.tenant_id,
        user_id=row.id,
        active=row.status == "active" and row.tenant_status == "active",
        capabilities=frozenset(
            CapabilityGrant(capability=cap, scope_department=scope)
            for _, cap, scope in caps
            if cap is not None
        ),
        group_ids=frozenset(gid for gid, _ in groups),
        clearance=row.clearance,
        country=row.country,
        department=row.department,
    )
