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
        description="auto: sales/period questions go to the verified SQL report, contract "
        "clause and expiry questions to the legal pack, repository code questions to the "
        "code pack, everything else to authorized document answers (enabled packs only). "
        "general (M8, explicit only): the model answers from general knowledge, without "
        "company documents, data or actions; never chosen by auto.",
    )
    client_request_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{8,64}$",
        description="Idempotency key: retrying with the same key returns the same run.",
    )


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
