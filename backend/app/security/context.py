"""Server-built access context. Never populated from request bodies or headers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.security.policy import Subject


@dataclass(frozen=True, slots=True)
class AccessContext:
    request_id: str | None
    session_id: UUID
    session_expires_at: datetime
    tenant_slug: str
    tenant_name: str
    policy_epoch: int
    email: str
    display_name: str
    role_keys: tuple[str, ...]
    group_keys: tuple[str, ...]
    subject: Subject

    @property
    def tenant_id(self) -> UUID:
        return self.subject.tenant_id

    @property
    def user_id(self) -> UUID:
        return self.subject.user_id
