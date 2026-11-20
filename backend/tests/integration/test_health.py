"""Liveness/readiness at the application boundary with real and broken dependencies."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.conftest import DbHarness, make_settings


def test_liveness_needs_no_dependencies(test_db: DbHarness) -> None:
    settings = make_settings(
        test_db,
        "http://127.0.0.1:1",
        DATABASE_URL="postgresql+psycopg://nobody:nopass@127.0.0.1:1/none",
        REDIS_URL="redis://127.0.0.1:1/0",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_when_dependencies_healthy(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "redis": "ok", "qdrant": "ok", "storage": "ok"},
        "model_inference": "not_configured",
    }


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        ({"REDIS_URL": "redis://127.0.0.1:1/0"}, "redis"),
        ({"QDRANT_URL": "http://127.0.0.1:1"}, "qdrant"),
        ({"S3_ENDPOINT_URL": "http://127.0.0.1:1"}, "storage"),
        ("bad_db_password", "postgres"),
    ],
)
def test_not_ready_when_a_dependency_fails(
    test_db: DbHarness, stub_http_url: str, override: object, failing: str
) -> None:
    if override == "bad_db_password":
        wrong = "definitely-not-the-password"
        override = {"DATABASE_URL": test_db.app_url.replace(test_db.secrets[1], wrong)}
        assert wrong in override["DATABASE_URL"]
    assert isinstance(override, dict)
    settings = make_settings(test_db, stub_http_url, **override)
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "not_ready"
    assert body["checks"][failing] == "unavailable"
    assert all(v == "ok" for k, v in body["checks"].items() if k != failing)
    for secret in test_db.secrets:
        assert secret not in response.text
    assert "127.0.0.1" not in response.text
    assert "definitely-not-the-password" not in response.text


def test_external_model_gateway_is_not_claimed_as_capability(
    test_db: DbHarness, stub_http_url: str
) -> None:
    settings = make_settings(test_db, stub_http_url, LLM_BASE_URL="http://127.0.0.1:1/v1")
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["model_inference"] == "external_unchecked"
