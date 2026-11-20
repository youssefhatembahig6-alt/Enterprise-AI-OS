"""Request/response schemas. Extra fields are rejected, so callers cannot smuggle
tenant, role, group, country or clearance values into a request."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictModel):
    tenant: str = Field(min_length=2, max_length=63, description="Tenant slug")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_at: datetime


class TenantOut(StrictModel):
    id: UUID
    slug: str
    name: str


class AttributesOut(StrictModel):
    clearance: str | None
    country: str | None
    department: str | None


class CapabilityOut(StrictModel):
    capability: str
    scope_department: str | None


class IdentityResponse(StrictModel):
    user_id: UUID
    email: str
    display_name: str
    tenant: TenantOut
    roles: list[str]
    capabilities: list[CapabilityOut]
    groups: list[str]
    attributes: AttributesOut
    policy_epoch: int
    session_expires_at: datetime


class ErrorBody(StrictModel):
    code: str
    message: str
    request_id: str | None


class ErrorResponse(StrictModel):
    error: ErrorBody


class LivenessResponse(StrictModel):
    status: Literal["ok"]


class ReadinessResponse(StrictModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, str]
    model_inference: Literal["not_configured", "external_unchecked", "fake"]
