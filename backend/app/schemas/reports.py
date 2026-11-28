"""Report API schemas (M2). See docs/contracts/reports.md for event payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.auth import StrictModel


class ChatRequest(StrictModel):
    question: str = Field(min_length=1, max_length=1000)
    mode: Literal["auto", "report", "documents", "legal", "code", "general"] = Field(
        default="auto",
        description="auto (the chat, M8): the server's orchestrator picks one handler: "
        "general for greetings and ordinary questions, documents for company policies and "
        "records, report for sales figures, legal for contract clauses and expiry, code for "
        "company repositories, among the tenant's enabled packs; a question too vague to "
        "route gets one short clarifying question and no run. Each handler keeps its own "
        "permission checks, verification and citations. An explicit mode (API clients and "
        "tests) runs that handler directly.",
    )
    client_request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
        description="Idempotency key: retrying with the same key returns the same run.",
    )
    conversation_id: UUID | None = Field(
        default=None,
        description="Append this exchange to one of the caller's own conversations; omit "
        "to start a new one. The conversation id is returned in X-Conversation-ID. A "
        "conversation that is not the caller's returns 404 and creates no run.",
    )


class ConversationOut(StrictModel):
    conversation_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationList(StrictModel):
    conversations: list[ConversationOut]


class TurnOut(StrictModel):
    turn_id: UUID
    question: str
    handler: Literal["general", "documents", "report", "legal", "code", "clarify"]
    handler_label: str
    run_id: UUID | None = Field(
        description="Replay the answer with GET /api/v1/runs/{run_id}/events: its content "
        "is re-authorized for the caller at that moment. Null for a clarifying question."
    )
    reply: str | None = Field(description="The clarifying question's text; null otherwise.")
    created_at: datetime


class ConversationDetail(ConversationOut):
    turns: list[TurnOut]


class RunOut(StrictModel):
    run_id: UUID
    status: Literal["queued", "running", "published", "abstained", "failed", "cancelled"]
    inference_mode: Literal["gateway", "fake"]
    outcome_code: str | None
    created_at: datetime
    finished_at: datetime | None
    last_event_id: int
    diagnostics: dict[str, Any] | None = Field(
        default=None,
        description="Stage timings, attempts, prompt version and served model revisions. "
        "Never contains drafts, rows or private authorization reasons.",
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="Published verified result; present only while the caller is still "
        "authorized for the report's dataset.",
    )


class EvidenceOut(StrictModel):
    evidence_id: UUID
    run_id: UUID
    report_key: str
    executed_sql: str
    parameters: dict[str, Any]
    columns: list[dict[str, str]]
    rows: list[list[Any]]
    row_count: int
    semantics: dict[str, Any]
    dataset_fingerprint: str
    policy_epoch: int
    executed_at: datetime
    result_sha256: str
    integrity: Literal["verified", "mismatch"]
