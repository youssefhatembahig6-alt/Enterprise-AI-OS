"""Document API schemas (M3). Uploads are JSON with base64 content (no multipart
dependency); the decoded size limit is 10 MB."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.auth import StrictModel


class GrantIn(StrictModel):
    effect: Literal["allow", "deny"] = "allow"
    group: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    user_email: str | None = Field(default=None, max_length=320)


class PermissionsIn(StrictModel):
    classification: Literal["public", "internal", "confidential", "restricted"]
    department_restricted: bool = False
    allowed_countries: list[str] = Field(default_factory=lambda: ["*"], min_length=1)
    grants: list[GrantIn] = Field(default_factory=list, max_length=50)


class UploadIn(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=14_000_000)
    department: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    title: str | None = Field(default=None, max_length=300)
    permissions: PermissionsIn | None = Field(
        default=None,
        description="Omit to store the document with unknown permissions: it is ingested "
        "but never searchable until permissions are set.",
    )


class VersionIn(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=14_000_000)


class ReindexIn(StrictModel):
    """M8: optional preconditions for a reindex (both refer to the current version)."""

    expected_version: int | None = Field(default=None, ge=1)
    expected_byte_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class UploadOut(StrictModel):
    document_id: str
    status: str


class CitationOut(StrictModel):
    run_id: str
    citation_id: str
    document_id: str
    title: str
    version: int
    current_version: int
    is_current_version: bool
    filename: str
    char_start: int
    char_end: int
    offset_unit: Literal["unicode_code_point"]
    text_sha256: str
    before: str
    quote: str
    after: str
    context_start: int
    integrity: Literal["verified", "mismatch"]
    # M5: repository citations (code pack) and any verbatim source.
    repository: str | None = None
    path: str | None = None
    commit: str | None = None
    blob_sha: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    url: str | None = None
