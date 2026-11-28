"""The committed OpenAPI file matches the implemented endpoints (no drift)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.main import create_app

PUBLISHED = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"


@pytest.mark.skipif(not PUBLISHED.exists(), reason="docs/ not present (container build)")
def test_published_openapi_matches_app() -> None:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://openapi@localhost/none",
        REDIS_URL="redis://localhost:6379/0",
        QDRANT_URL="http://localhost:6333",
        S3_ENDPOINT_URL="http://localhost:8333",
        JWT_SECRET="openapi-export-only-not-a-real-secret-000",
    )
    generated = create_app(settings).openapi()
    assert json.loads(PUBLISHED.read_text()) == generated, (
        "docs/openapi.json is stale: run python scripts/dev.py openapi"
    )
    assert set(generated["paths"]) == {
        "/health",
        "/ready",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/me",
        "/api/v1/chat",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/cancel",
        "/api/v1/evidence/{evidence_id}",
        "/api/v1/model/status",
        "/api/v1/documents/{document_id}/jobs/{job_id}",
        "/api/v1/documents",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/permissions",
        "/api/v1/documents/{document_id}/versions",
        "/api/v1/documents/{document_id}/reindex",
        "/api/v1/runs/{run_id}/citations/{citation_id}",
        # M4
        "/api/v1/tenant/settings",
        "/api/v1/admin/tenant/settings",
        "/api/v1/admin/users",
        "/api/v1/admin/users/{user_id}",
        "/api/v1/admin/users/{user_id}/roles",
        "/api/v1/admin/users/{user_id}/groups",
        "/api/v1/admin/roles",
        "/api/v1/admin/roles/{role_key}",
        "/api/v1/admin/groups",
        "/api/v1/admin/groups/{group_key}/links",
        "/api/v1/admin/documents",
        "/api/v1/admin/sources",
        "/api/v1/admin/sources/{source_id}",
        "/api/v1/admin/sources/{source_id}/sync",
        "/api/v1/admin/sources/{source_id}/runs/{run_id}",
        "/api/v1/admin/audit",
        # M5
        "/api/v1/admin/users/{user_id}/external-accounts",
        "/api/v1/local-folders",
        "/api/v1/local-folders/{source_id}",
        "/api/v1/local-folders/{source_id}/sharing",
        "/api/v1/local-folders/{source_id}/items",
        "/api/v1/local-folders/{source_id}/events",
        # M6
        "/api/v1/actions/recipients",
        "/api/v1/actions/email-drafts",
        "/api/v1/actions/email-drafts/{action_id}",
        "/api/v1/actions/email-drafts/{action_id}/approve",
        "/api/v1/actions/email-drafts/{action_id}/send",
        "/api/v1/actions/email-drafts/{action_id}/cancel",
        "/api/v1/actions/briefings",
        "/api/v1/actions/briefings/{schedule_id}/deactivate",
    }
    chat = generated["paths"]["/api/v1/chat"]["post"]["responses"]["200"]["content"]
    assert "text/event-stream" in chat
