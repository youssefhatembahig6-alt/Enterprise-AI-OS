"""Ingestion safety on real PostgreSQL and Qdrant: permissions before scheduling,
useful failure statuses, crash/retry without duplicates or partial publication,
reindex, versions, deletion tombstones and distinct identities."""

from __future__ import annotations

import base64
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.conftest import (
    DbHarness,
    disposable_database,
    document_settings,
    login,
    owner_exec,
)
from tests.integration.document_support import (
    FIXTURES,
    auth,
    corpus_doc,
    upload,
    upload_body,
    wait_status,
)
from tests.integration.report_support import ask

QUESTION = "How many invoices were overdue at the reference date?"


class Crash(Exception):
    """Simulated worker crash at an ingestion stage."""


@pytest.fixture(scope="module")
def ingest_db() -> Iterator[DbHarness]:
    with disposable_database(seed=True) as db:
        yield db


@pytest.fixture(scope="module")
def module_client(
    ingest_db: DbHarness,
    stub_http_url: str,
    qdrant_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    """One app and one Qdrant collection for the module (tests use distinct uploads)."""

    settings = document_settings(
        ingest_db,
        stub_http_url,
        qdrant_url,
        str(tmp_path_factory.mktemp("ingest")),
        "ti" + uuid.uuid4().hex[:8],
    )
    with TestClient(create_app(settings)) as client:
        yield client
        client.app.state.documents._qdrant.delete_collection(  # type: ignore[attr-defined]
            client.app.state.report_runner.documents_workflow.collection()  # type: ignore[attr-defined]
        )


@pytest.fixture
def app_client(module_client: TestClient, ingest_db: DbHarness) -> Iterator[TestClient]:
    yield module_client
    runner(module_client).fault = None
    # Tombstone this test's documents so later tests start from an empty corpus.
    owner_exec(ingest_db, "UPDATE resources SET status = 'deleted' WHERE kind = 'document'")
    owner_exec(ingest_db, "UPDATE documents SET deleted_at = now() WHERE deleted_at IS NULL")
    owner_exec(ingest_db, "UPDATE ingestion_jobs SET status = 'done' WHERE status <> 'done'")


def runner(client: TestClient) -> Any:
    return client.app.state.ingestion_runner  # type: ignore[attr-defined]


def qdrant_count(client: TestClient, document_id: str, generation: int | None = None) -> int:
    must: list[dict[str, Any]] = [{"key": "document_id", "match": {"value": document_id}}]
    if generation is not None:
        must.append({"key": "generation", "match": {"value": generation}})
    wf = client.app.state.report_runner.documents_workflow  # type: ignore[attr-defined]
    return int(wf.qdrant.count(wf.collection(), {"must": must}))


def job_for(db: DbHarness, document_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    row = owner_exec(
        db,
        "SELECT id, tenant_id FROM ingestion_jobs WHERE document_id = :d"
        " ORDER BY created_at DESC LIMIT 1",
        {"d": document_id},
    ).one()
    return row.id, row.tenant_id


def expire_lease(db: DbHarness, job_id: uuid.UUID) -> None:
    owner_exec(
        db,
        "UPDATE ingestion_jobs SET claimed_until = now() - interval '1 second' WHERE id = :j",
        {"j": job_id},
    )


CRASHED = threading.Event()


def crash_at(client: TestClient, stage: str, times: int = 1) -> list[str]:
    """Make the next ``times`` passes through ``stage`` raise; ``CRASHED`` is set just
    before the simulated crash so tests wait on the event, not on sleeps."""

    seen: list[str] = []
    CRASHED.clear()

    def fault(name: str) -> None:
        seen.append(name)
        if name == stage and seen.count(stage) <= times:
            CRASHED.set()
            raise Crash(stage)

    runner(client).fault = fault
    return seen


def wait_crashed(db: DbHarness, job_id: uuid.UUID) -> None:
    assert CRASHED.wait(10), "the simulated crash did not happen"
    time.sleep(0.05)  # let the worker thread unwind
    assert (
        owner_exec(
            db, "SELECT status FROM ingestion_jobs WHERE id = :j", {"j": job_id}
        ).scalar_one()
        == "claimed"
    )


def generations(db: DbHarness, document_id: str) -> list[tuple[int, str, int]]:
    rows = owner_exec(
        db,
        "SELECT generation, status, chunk_count FROM document_generations"
        " WHERE document_id = :d ORDER BY generation",
        {"d": document_id},
    ).all()
    return [(r.generation, r.status, r.chunk_count) for r in rows]


def finance(db: DbHarness, client: TestClient) -> str:
    return upload(client, db, corpus_doc("finance-q1-commentary"))


# -- permissions before scheduling, authorization of uploads ----------------------------------


def test_permissions_are_committed_before_the_job_is_dispatched(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    service = app_client.app.state.documents  # type: ignore[attr-defined]
    original = service._dispatch
    observed: list[Any] = []

    def spy(job_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        row = owner_exec(
            ingest_db,
            "SELECT r.permission_state, (SELECT count(*) FROM resource_grants g WHERE g.resource_id = r.id)"
            " AS grants, j.status FROM ingestion_jobs j JOIN resources r ON r.id = j.document_id"
            " WHERE j.id = :j",
            {"j": job_id},
        ).one()
        observed.append(row)
        original(job_id, tenant_id)

    service._dispatch = spy
    try:
        document_id = finance(ingest_db, app_client)
    finally:
        service._dispatch = original
    assert len(observed) == 1
    assert observed[0].permission_state == "verified" and observed[0].grants == 2
    assert observed[0].status == "pending"
    wait_status(ingest_db, document_id, {"ready"})


@pytest.mark.parametrize(
    ("ref", "department", "status"),
    [
        ("niletech/finance-admin", "finance", 202),
        ("niletech/finance-admin", "hr", 403),
        ("niletech/finance-reader", "finance", 403),
        ("isolation-test/finance-admin", "finance", 202),
    ],
)
def test_upload_requires_admin_for_the_department(
    app_client: TestClient, ingest_db: DbHarness, ref: str, department: str, status: int
) -> None:
    body = upload_body(ingest_db, corpus_doc("it-security-handbook"))
    body["department"] = department
    response = app_client.post(
        "/api/v1/documents", json=body, headers=auth(login(app_client, ingest_db, ref))
    )
    assert response.status_code == status, response.text


# -- useful statuses for malformed and unsupported files -------------------------------------


@pytest.mark.parametrize(
    ("filename", "data", "status", "code"),
    [
        ("broken.pdf", b"%PDF-1.7\nthis is not a real pdf body", "failed", "malformed_file"),
        ("broken.xlsx", b"PK\x03\x04 not a workbook", "failed", "malformed_file"),
        ("empty.md", b"\n\n", "failed", "empty_document"),
        (
            "scanned-delivery-note.pdf",
            (FIXTURES / "files" / "scanned-delivery-note.pdf").read_bytes(),
            "needs_ocr",
            "needs_ocr",
        ),
    ],
)
def test_bad_files_end_with_a_useful_status(
    app_client: TestClient, ingest_db: DbHarness, filename: str, data: bytes, status: str, code: str
) -> None:
    admin = login(app_client, ingest_db, "niletech/access-admin")
    body = {
        "filename": filename,
        "content_base64": base64.b64encode(data).decode(),
        "department": "it",
        "permissions": {"classification": "internal", "grants": [{"group": "all-staff"}]},
    }
    response = app_client.post("/api/v1/documents", json=body, headers=auth(admin))
    assert response.status_code == 202
    document_id = response.json()["document_id"]
    assert wait_status(ingest_db, document_id, {status}) == status
    detail = app_client.get(f"/api/v1/documents/{document_id}", headers=auth(admin)).json()
    assert detail["status"] == status and detail["error"]["code"] == code
    assert detail["error"]["message"] and detail["searchable"] is False
    assert qdrant_count(app_client, document_id) == 0


def test_unsupported_type_is_rejected_at_upload(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    admin = login(app_client, ingest_db, "niletech/access-admin")
    body = {
        "filename": "notes.docx",
        "content_base64": base64.b64encode(b"PK\x03\x04").decode(),
        "department": "it",
    }
    response = app_client.post("/api/v1/documents", json=body, headers=auth(admin))
    assert response.status_code == 422 and response.json()["error"]["code"] == "unsupported_type"


# -- crash, retry and partial publication ----------------------------------------------------


@pytest.mark.parametrize("stage", ["after_chunks", "after_upsert", "before_activate"])
def test_crash_then_retry_publishes_once_without_duplicates(
    app_client: TestClient, ingest_db: DbHarness, stage: str
) -> None:
    crash_at(app_client, stage)
    document_id = finance(ingest_db, app_client)
    job_id, tenant_id = job_for(ingest_db, document_id)
    # The crashed job stays claimed; nothing is active, so nothing is searchable.
    wait_crashed(ingest_db, job_id)
    assert [g[1] for g in generations(ingest_db, document_id)] == ["staging"]
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    assert ask(app_client, reader, QUESTION, mode="documents").done["outcome"] == "abstained"
    # A duplicate delivery while the lease is held is a no-op.
    assert runner(app_client).run_job(job_id, tenant_id) == "not_claimed"
    expire_lease(ingest_db, job_id)
    assert runner(app_client).run_job(job_id, tenant_id) == "ready"
    gens = generations(ingest_db, document_id)
    assert len(gens) == 1 and gens[0][1] == "active"
    chunks = gens[0][2]
    in_db = owner_exec(
        ingest_db,
        "SELECT count(*), count(DISTINCT point_id) FROM document_chunks WHERE document_id = :d",
        {"d": document_id},
    ).one()
    assert tuple(in_db) == (chunks, chunks)
    assert qdrant_count(app_client, document_id) == chunks
    assert ask(app_client, reader, QUESTION, mode="documents").done["outcome"] == "published"
    runner(app_client).fault = None


def test_transient_embedding_failure_is_retried(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    from app.reporting.model_client import FakeModelClient, GatewayUnavailable

    ingestion = runner(app_client)
    real = ingestion._model
    ingestion._model = FakeModelClient(embed_error=GatewayUnavailable("gateway down"))
    try:
        document_id = finance(ingest_db, app_client)
        job_id, tenant_id = job_for(ingest_db, document_id)
        for _ in range(100):
            if owner_exec(
                ingest_db, "SELECT attempts FROM ingestion_jobs WHERE id = :j", {"j": job_id}
            ).scalar_one():
                break
            time.sleep(0.02)
        time.sleep(0.2)
        assert wait_status(ingest_db, document_id, {"queued"}) == "queued"
        reason = owner_exec(
            ingest_db,
            "SELECT error_code FROM document_versions WHERE document_id = :d",
            {"d": document_id},
        ).scalar_one()
        assert reason == "embedding_unavailable"
        assert (
            owner_exec(
                ingest_db, "SELECT status FROM ingestion_jobs WHERE id = :j", {"j": job_id}
            ).scalar_one()
            == "pending"
        )
    finally:
        ingestion._model = real
    expire_lease(ingest_db, job_id)
    assert ingestion.run_job(job_id, tenant_id) == "ready"
    assert [g[1] for g in generations(ingest_db, document_id)] == ["active"]
    assert (
        owner_exec(
            ingest_db,
            "SELECT error_code FROM document_versions WHERE document_id = :d",
            {"d": document_id},
        ).scalar_one()
        is None
    )


def test_reindex_keeps_the_old_generation_searchable_until_the_new_one_is_complete(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    document_id = finance(ingest_db, app_client)
    wait_status(ingest_db, document_id, {"ready"})
    first_points = qdrant_count(app_client, document_id, 1)
    admin = login(app_client, ingest_db, "niletech/access-admin")
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    crash_at(app_client, "before_activate")
    assert (
        app_client.post(f"/api/v1/documents/{document_id}/reindex", headers=auth(admin)).status_code
        == 202
    )
    wait_crashed(ingest_db, job_for(ingest_db, document_id)[0])
    assert qdrant_count(app_client, document_id, 2) == first_points
    assert [g[1] for g in generations(ingest_db, document_id)] == ["active", "staging"]
    stream = ask(app_client, reader, QUESTION, mode="documents")
    assert stream.done["outcome"] == "published"
    cited_generation = owner_exec(
        ingest_db, "SELECT generation FROM answer_sources WHERE run_id = :r", {"r": stream.run_id}
    ).scalar_one()
    assert cited_generation == 1  # never the staging generation
    job_id, tenant_id = job_for(ingest_db, document_id)
    expire_lease(ingest_db, job_id)
    runner(app_client).fault = None
    assert runner(app_client).run_job(job_id, tenant_id) == "ready"
    assert [g[1] for g in generations(ingest_db, document_id)] == ["retired", "active"]
    assert qdrant_count(app_client, document_id, 1) == 0  # retired points cleaned up
    assert qdrant_count(app_client, document_id, 2) == first_points


def test_new_version_activates_and_old_citations_open_the_cited_version(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    document_id = finance(ingest_db, app_client)
    wait_status(ingest_db, document_id, {"ready"})
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    old = ask(app_client, reader, QUESTION, mode="documents")
    old_cid = old.of("citation")[0].data["citation_id"]
    original = (FIXTURES / "files" / "finance-q1-commentary.md").read_text()
    revised = original.replace("370 open invoices", "412 open invoices")
    admin = login(app_client, ingest_db, "niletech/access-admin")
    response = app_client.post(
        f"/api/v1/documents/{document_id}/versions",
        json={
            "filename": "finance-q1-commentary.md",
            "content_base64": base64.b64encode(revised.encode()).decode(),
        },
        headers=auth(admin),
    )
    assert response.status_code == 202 and response.json()["version"] == 2
    wait_status(ingest_db, document_id, {"ready"})
    new = ask(app_client, reader, QUESTION, mode="documents")
    assert "412 open invoices" in new.of("citation")[0].data["quote"]
    reopened = app_client.get(
        f"/api/v1/runs/{old.run_id}/citations/{old_cid}", headers=auth(reader)
    ).json()
    assert reopened["version"] == 1 and reopened["current_version"] == 2
    assert reopened["is_current_version"] is False
    assert "370 open invoices" in reopened["quote"] and reopened["integrity"] == "verified"


# -- deletion tombstones -------------------------------------------------------------------


def test_deleted_documents_are_unsearchable_and_late_retries_cannot_resurrect_them(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    crash_at(app_client, "before_activate")
    document_id = finance(ingest_db, app_client)
    job_id, tenant_id = job_for(ingest_db, document_id)
    wait_crashed(ingest_db, job_id)
    assert [g[1] for g in generations(ingest_db, document_id)] == ["staging"]
    assert qdrant_count(app_client, document_id) > 0  # points exist but are not eligible
    admin = login(app_client, ingest_db, "niletech/access-admin")
    assert (
        app_client.delete(f"/api/v1/documents/{document_id}", headers=auth(admin)).status_code
        == 204
    )
    expire_lease(ingest_db, job_id)
    runner(app_client).fault = None
    assert runner(app_client).run_job(job_id, tenant_id) == "deleted"
    assert (
        owner_exec(
            ingest_db, "SELECT active_generation FROM documents WHERE id = :d", {"d": document_id}
        ).scalar_one()
        is None
    )
    assert qdrant_count(app_client, document_id) == 0
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    assert (
        app_client.get(f"/api/v1/documents/{document_id}", headers=auth(reader)).status_code == 404
    )


def test_deleting_an_active_document_removes_it_from_answers_and_citations(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    document_id = finance(ingest_db, app_client)
    wait_status(ingest_db, document_id, {"ready"})
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    before = ask(app_client, reader, QUESTION, mode="documents")
    cid = before.of("citation")[0].data["citation_id"]
    admin = login(app_client, ingest_db, "niletech/access-admin")
    assert (
        app_client.delete(f"/api/v1/documents/{document_id}", headers=auth(admin)).status_code
        == 204
    )
    after = ask(app_client, reader, QUESTION, mode="documents")
    assert all(c.data["document_id"] != document_id for c in after.of("citation"))
    assert (
        app_client.get(
            f"/api/v1/runs/{before.run_id}/citations/{cid}", headers=auth(reader)
        ).status_code
        == 404
    )
    assert qdrant_count(app_client, document_id) == 0


# -- distinct identities -----------------------------------------------------------------------


def test_identical_bytes_under_other_tenants_or_permissions_stay_distinct(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    doc = corpus_doc("it-security-handbook")
    open_copy = upload(app_client, ingest_db, doc)
    restricted = dict(
        doc,
        permissions=dict(
            doc["permissions"],
            classification="restricted",
            grants=[{"user": "board-member", "effect": "allow"}],
        ),
    )
    restricted_copy = upload(app_client, ingest_db, restricted)
    isolation = dict(
        doc,
        tenant="isolation-test",
        permissions=dict(
            doc["permissions"],
            department="finance",
            grants=[{"group": "all-staff", "effect": "allow"}],
        ),
    )
    other_tenant = upload(app_client, ingest_db, isolation)
    ids = [open_copy, restricted_copy, other_tenant]
    for document_id in ids:
        wait_status(ingest_db, document_id, {"ready"})
    assert len(set(ids)) == 3
    hashes = (
        owner_exec(
            ingest_db,
            "SELECT DISTINCT byte_sha256 FROM document_versions WHERE document_id = ANY(CAST(:i AS uuid[]))",
            {"i": ids},
        )
        .scalars()
        .all()
    )
    assert len(hashes) == 1  # same bytes...
    points = owner_exec(
        ingest_db,
        "SELECT document_id::text, point_id::text FROM document_chunks"
        " WHERE document_id = ANY(CAST(:i AS uuid[]))",
        {"i": ids},
    ).all()
    by_doc: dict[str, set[str]] = {}
    for row in points:
        by_doc.setdefault(row.document_id, set()).add(row.point_id)
    assert len(by_doc) == 3
    assert not (by_doc[open_copy] & by_doc[restricted_copy]) and not (
        by_doc[open_copy] & by_doc[other_tenant]
    )
    # ...but separate permissions: the sales reader sees only the open copy.
    reader = login(app_client, ingest_db, "niletech/sales-reader")
    stream = ask(app_client, reader, "What is the minimum password length?", mode="documents")
    cited = {c.data["document_id"] for c in stream.of("citation")}
    assert cited <= {open_copy} and cited
