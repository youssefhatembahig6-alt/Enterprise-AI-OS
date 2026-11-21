"""Helpers for M3 document tests: corpus seeding through the real API, ingestion
waits, Qdrant request capture and leak assertions."""

from __future__ import annotations

import base64
import time
import tomllib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.conftest import DbHarness, login

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "documents"
CORPUS = tomllib.loads((FIXTURES / "corpus.toml").read_text())
NILETECH_MARKERS = [
    d["marker"] for d in CORPUS["documents"] if d["tenant"] == "niletech" and "marker" in d
]
UPLOADER = {"niletech": "niletech/access-admin", "isolation-test": "isolation-test/finance-admin"}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def corpus_doc(key: str) -> dict[str, Any]:
    return next(d for d in CORPUS["documents"] if d["key"] == key)


def upload_body(
    db: DbHarness, doc: dict[str, Any], *, data: bytes | None = None, permissions: bool = True
) -> dict[str, Any]:
    raw = data if data is not None else (FIXTURES / "files" / doc["file"]).read_bytes()
    body: dict[str, Any] = {
        "filename": doc["file"],
        "content_base64": base64.b64encode(raw).decode(),
        "department": (doc.get("permissions") or {}).get("department", "finance"),
        "title": doc["title"],
    }
    perms = doc.get("permissions")
    if perms and permissions:
        tenant = db.foundation.tenants[doc["tenant"]]
        grants = []
        for g in perms["grants"]:
            if "group" in g:
                grants.append({"effect": g["effect"], "group": g["group"]})
            else:
                grants.append({"effect": g["effect"], "user_email": tenant.users[g["user"]].email})
        body["permissions"] = {
            "classification": perms["classification"],
            "department_restricted": perms["department_restricted"],
            "allowed_countries": perms["allowed_countries"],
            "grants": grants,
        }
    return body


def upload(client: TestClient, db: DbHarness, doc: dict[str, Any], **kw: Any) -> str:
    token = login(client, db, UPLOADER[doc["tenant"]])
    response = client.post(
        "/api/v1/documents", json=upload_body(db, doc, **kw), headers=auth(token)
    )
    assert response.status_code == 202, response.text
    return str(response.json()["document_id"])


def wait_status(db: DbHarness, document_id: str, wanted: set[str], timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    status = "?"
    while time.monotonic() < deadline:
        with db.owner_engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT v.status FROM document_versions v JOIN documents d"
                    " ON d.id = v.document_id AND v.version = d.current_version WHERE d.id = :d"
                ),
                {"d": document_id},
            ).scalar_one()
        if status in wanted:
            return str(status)
        time.sleep(0.05)
    raise AssertionError(f"document {document_id} stuck in {status}")


def seed_corpus(client: TestClient, db: DbHarness) -> dict[str, str]:
    ids: dict[str, str] = {}
    for doc in CORPUS["documents"]:
        ids[doc["key"]] = upload(client, db, doc)
    for doc in CORPUS["documents"]:
        wait_status(db, ids[doc["key"]], {doc.get("expect_status", "ready")})
    return ids
