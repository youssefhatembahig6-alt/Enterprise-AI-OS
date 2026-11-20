"""Database-backed authorization decisions using the reference evaluator.

Resource metadata and grants are loaded under the caller's tenant context, so a
resource from another tenant is simply not found (reported as ``deny.not_found``
in the private audit reason; callers must not distinguish it from absence).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Engine

from app.db.engine import tenant_transaction
from app.repositories.audit import record_audit_event
from app.repositories.resources import load_resource_meta
from app.security.context import AccessContext
from app.security.policy import Decision, Reason, evaluate


def authorize_resource(
    engine: Engine, ctx: AccessContext, action: str, resource_id: UUID
) -> Decision:
    with tenant_transaction(engine, ctx.tenant_id, ctx.user_id) as conn:
        resource = load_resource_meta(conn, resource_id)
        decision = (
            evaluate(ctx.subject, action, resource)
            if resource is not None
            else Decision.deny(Reason.NOT_FOUND)
        )
        record_audit_event(
            conn,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type="authz.decision",
            action=action,
            resource_id=resource_id if resource is not None else None,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason.value,
            details={"policy_epoch": ctx.policy_epoch},
        )
    return decision
