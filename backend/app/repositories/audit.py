"""Audit event persistence (insert-only for the application role)."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.logging_setup import redact_value

logger = logging.getLogger("eaios.audit")

_INSERT = text(
    """
    INSERT INTO audit_events
        (request_id, tenant_id, user_id, event_type, action, resource_id, decision, reason,
         details)
    VALUES (:request_id, :tenant_id, :user_id, :event_type, :action, :resource_id, :decision,
            :reason, CAST(:details AS jsonb))
    """
)


def record_audit_event(
    conn: Connection,
    *,
    request_id: str | None,
    event_type: str,
    action: str,
    decision: str,
    reason: str,
    tenant_id: UUID | None = None,
    user_id: UUID | None = None,
    resource_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write an audit row in the caller's transaction and emit a structured log line.

    ``details`` must never contain passwords, tokens, keys or document text; a
    redaction pass is applied as a second line of defence.
    """

    safe_details = redact_value("details", details or {})
    conn.execute(
        _INSERT,
        {
            "request_id": request_id,
            "tenant_id": str(tenant_id) if tenant_id else None,
            "user_id": str(user_id) if user_id else None,
            "event_type": event_type,
            "action": action,
            "resource_id": str(resource_id) if resource_id else None,
            "decision": decision,
            "reason": reason,
            "details": json.dumps(safe_details),
        },
    )
    logger.info(
        "audit_event",
        extra={
            "event_type": event_type,
            "action": action,
            "decision": decision,
            "reason": reason,
            "tenant_id": str(tenant_id) if tenant_id else None,
            "user_id": str(user_id) if user_id else None,
            "resource_id": str(resource_id) if resource_id else None,
        },
    )
