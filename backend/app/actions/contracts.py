"""Strict M6 request/response models.

Identity, tenant, sender and recipient addresses are never client supplied: the
recipient is a user id resolved server-side against the tenant's user records, and the
approval binds to the authenticated session. Extra fields are rejected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftCreate(StrictRequest):
    source_run_id: UUID
    recipient_user_id: UUID


class VersionRef(StrictRequest):
    """What the user saw: the exact version number and its content hash."""

    version: int = Field(ge=1)
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DraftEdit(VersionRef):
    recipient_user_id: UUID
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=50000)

    @field_validator("subject")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Subject must be one line")
        return value


class SendRequest(VersionRef):
    approval_token: str = Field(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


class ScheduleCreate(StrictRequest):
    question: str = Field(min_length=1, max_length=2000)
    recipient_user_id: UUID
    interval_seconds: int = Field(ge=3600, le=2678400)
    first_run_at: datetime

    @field_validator("first_run_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value


class RecipientOut(BaseModel):
    user_id: UUID
    display_name: str
    email: str


class ApprovalOut(BaseModel):
    approval_token: str
    action_id: UUID
    version: int
    draft_sha256: str
    expires_at: datetime


class DeliveryOut(BaseModel):
    execution_key: UUID
    message_id: str
    status: str
    status_reason: str | None = None
    attempts: int
    smtp_accepted_at: datetime | None = None
    confirmed_at: datetime | None = None
    uncertain_since: datetime | None = None
    mailpit_id: str | None = None


class DraftOut(BaseModel):
    action_id: UUID
    status: str
    status_reason: str | None = None
    origin: str
    source_run_id: UUID
    version: int
    draft_sha256: str
    recipient: RecipientOut
    subject: str | None
    body: str | None
    content_withheld: bool = False
    edited: bool
    approval_expires_at: datetime | None = None
    delivery: DeliveryOut | None = None
    created_at: datetime
    updated_at: datetime


class OccurrenceOut(BaseModel):
    occurrence_id: UUID
    due_at: datetime
    status: str
    status_reason: str | None = None
    run_id: UUID | None = None
    action_id: UUID | None = None


class ScheduleOut(BaseModel):
    schedule_id: UUID
    question: str
    recipient: RecipientOut
    interval_seconds: int
    next_run_at: datetime
    active: bool
    created_at: datetime
    occurrences: list[OccurrenceOut] = []


def as_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
