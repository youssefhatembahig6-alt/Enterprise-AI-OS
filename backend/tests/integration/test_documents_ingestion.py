"""Ingestion safety on real PostgreSQL and Qdrant: permissions before scheduling,
useful failure statuses, crash/retry without duplicates or partial publication,
reindex, versions, deletion tombstones and distinct identities."""

from __future__ import annotations

import base64
import hashlib
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


# -- B2: the document list never shows an ingestion commit half applied ---------------------


def listed(client: TestClient, token: str, document_id: str) -> dict[str, Any] | None:
    response = client.get("/api/v1/documents", headers=auth(token))
    assert response.status_code == 200, response.text
    return next((d for d in response.json() if d["document_id"] == document_id), None)


@pytest.mark.parametrize("endpoint", ["list", "detail"])
def test_activation_committing_mid_request_is_never_reported_ready_but_unsearchable(
    app_client: TestClient,
    ingest_db: DbHarness,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """B2 (docs/evidence/m7-functional/README.md). ``_describe`` reads the document row
    (``active_generation``) and then, in a later statement, the versions (``status``). The
    activation transaction sets both. Under READ COMMITTED a commit between the two reads
    produced ``ready`` + ``searchable: false``, a terminal state for the UI's poller, so a
    correctly indexed upload kept the label "Not searchable" until the next sign-in.

    The interleaving is forced deterministically: the job is held just before activation,
    then committed from inside the request, between the two reads."""

    from app.documents import store

    crash_at(app_client, "before_activate")
    document_id = finance(ingest_db, app_client)
    job_id, tenant_id = job_for(ingest_db, document_id)
    wait_crashed(ingest_db, job_id)
    expire_lease(ingest_db, job_id)
    runner(app_client).fault = None

    real_list_versions = store.list_versions
    committed: list[str] = []

    def commit_activation_first(conn: Any, doc_id: uuid.UUID) -> Any:
        if str(doc_id) == document_id and not committed:
            committed.append(runner(app_client).run_job(job_id, tenant_id))
        return real_list_versions(conn, doc_id)

    monkeypatch.setattr(store, "list_versions", commit_activation_first)
    admin = login(app_client, ingest_db, "niletech/access-admin")
    if endpoint == "list":
        first = listed(app_client, admin, document_id)
    else:
        first = app_client.get(f"/api/v1/documents/{document_id}", headers=auth(admin)).json()
    assert committed == ["ready"], "the activation did not commit inside the request"
    assert first is not None
    # One snapshot: the state from before the commit, still in progress, so the UI keeps
    # polling. Never the mixed ("ready", False).
    assert (first["status"], first["searchable"]) == ("indexing", False)

    monkeypatch.setattr(store, "list_versions", real_list_versions)
    after = listed(app_client, admin, document_id)
    assert after is not None
    assert (after["status"], after["searchable"], after["permission_state"]) == (
        "ready",
        True,
        "verified",
    )


def test_unsearchable_states_are_still_reported(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    """The B2 fix keeps the genuine reasons a document is not searchable: sharing not
    set, failed ingestion, and a reader whose access was revoked."""

    admin = login(app_client, ingest_db, "niletech/access-admin")
    reader = login(app_client, ingest_db, "niletech/finance-reader")

    unshared = upload(app_client, ingest_db, corpus_doc("finance-q1-commentary"), permissions=False)
    failed_body = {
        "filename": "empty.md",
        "content_base64": base64.b64encode(b"\n\n").decode(),
        "department": "it",
        "permissions": {"classification": "internal", "grants": [{"group": "all-staff"}]},
    }
    response = app_client.post("/api/v1/documents", json=failed_body, headers=auth(admin))
    assert response.status_code == 202
    failed = response.json()["document_id"]
    shared = finance(ingest_db, app_client)
    wait_status(ingest_db, unshared, {"ready"})
    wait_status(ingest_db, failed, {"failed"})
    wait_status(ingest_db, shared, {"ready"})

    no_sharing = listed(app_client, admin, unshared)
    assert no_sharing is not None
    assert (no_sharing["status"], no_sharing["permission_state"], no_sharing["searchable"]) == (
        "ready",
        "unknown",
        False,
    )
    broken = listed(app_client, admin, failed)
    assert broken is not None
    assert (broken["status"], broken["searchable"], broken["error"]["code"]) == (
        "failed",
        False,
        "empty_document",
    )

    assert listed(app_client, reader, unshared) is None
    granted = listed(app_client, reader, shared)
    assert granted is not None and granted["searchable"] is True
    owner_exec(ingest_db, "DELETE FROM resource_grants WHERE resource_id = :d", {"d": shared})
    assert listed(app_client, reader, shared) is None
    assert app_client.get(f"/api/v1/documents/{shared}", headers=auth(reader)).status_code == 404


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


def jobs_of(db: DbHarness, document_id: str) -> int:
    return int(
        owner_exec(
            db, "SELECT count(*) FROM ingestion_jobs WHERE document_id = :d", {"d": document_id}
        ).scalar_one()
    )


def test_conditional_reindex_sends_only_the_verified_current_bytes(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    """M8: the connect script reindexes a fixture only if its current version still holds
    the fixture's exact bytes; the backend re-checks that under the row lock."""

    document_id = finance(ingest_db, app_client)
    wait_status(ingest_db, document_id, {"ready"})
    admin = login(app_client, ingest_db, "niletech/access-admin")
    original = (FIXTURES / "files" / "finance-q1-commentary.md").read_bytes()
    fixture_sha = hashlib.sha256(original).hexdigest()
    described = app_client.get(f"/api/v1/documents/{document_id}", headers=auth(admin)).json()
    assert [(v["version"], v["byte_sha256"]) for v in described["versions"]] == [(1, fixture_sha)]
    expect_v1 = {"expected_version": 1, "expected_byte_sha256": fixture_sha}
    url = f"/api/v1/documents/{document_id}/reindex"
    before = jobs_of(ingest_db, document_id)
    assert app_client.post(url, json=expect_v1, headers=auth(admin)).status_code == 202
    assert jobs_of(ingest_db, document_id) == before + 1
    wait_status(ingest_db, document_id, {"ready"})
    # The fixture is replaced by a new version with different content.
    revised = original.replace(b"370 open invoices", b"412 open invoices")
    response = app_client.post(
        f"/api/v1/documents/{document_id}/versions",
        json={
            "filename": "finance-q1-commentary.md",
            "content_base64": base64.b64encode(revised).decode(),
        },
        headers=auth(admin),
    )
    assert response.status_code == 202
    wait_status(ingest_db, document_id, {"ready"})
    queued = jobs_of(ingest_db, document_id)
    for stale in (
        expect_v1,  # the old version is no longer current
        {"expected_version": 2, "expected_byte_sha256": fixture_sha},  # content differs
        {"expected_byte_sha256": fixture_sha},
    ):
        refused = app_client.post(url, json=stale, headers=auth(admin))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "document_changed"
    assert jobs_of(ingest_db, document_id) == queued  # nothing was queued for embedding
    # Unconditional reindex keeps working as before.
    assert app_client.post(url, headers=auth(admin)).status_code == 202


def test_job_outcome_stays_pending_while_a_delayed_worker_leaves_the_old_index_active(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    """M8 regression: reindex must wait for ITS job. While the job is stalled before
    activation, the document still looks ready and searchable (old generation), but the
    job outcome is pending; it turns active (current embedding space) only after the new
    generation is activated."""

    document_id = finance(ingest_db, app_client)
    wait_status(ingest_db, document_id, {"ready"})
    admin = login(app_client, ingest_db, "niletech/access-admin")
    crash_at(app_client, "before_activate")
    queued = app_client.post(f"/api/v1/documents/{document_id}/reindex", headers=auth(admin))
    assert queued.status_code == 202
    job_id = queued.json()["job_id"]
    wait_crashed(ingest_db, uuid.UUID(job_id))
    described = app_client.get(f"/api/v1/documents/{document_id}", headers=auth(admin)).json()
    assert (described["status"], described["searchable"]) == ("ready", True)  # old index serves
    job_url = f"/api/v1/documents/{document_id}/jobs/{job_id}"
    stalled = app_client.get(job_url, headers=auth(admin)).json()
    assert stalled["outcome"] == "pending"
    assert stalled["in_current_space"] is False
    assert stalled["active_generation"] == 1 and stalled["generation"] == 2
    assert stalled["active_in_current_space"] is True  # the OLD generation is what serves
    # The worker recovers and activates the new generation.
    db_job, tenant_id = job_for(ingest_db, document_id)
    assert str(db_job) == job_id
    expire_lease(ingest_db, db_job)
    runner(app_client).fault = None
    assert runner(app_client).run_job(db_job, tenant_id) == "ready"
    done = app_client.get(job_url, headers=auth(admin)).json()
    assert (done["status"], done["outcome"], done["in_current_space"]) == ("done", "active", True)
    assert done["active_generation"] == 2 and done["chunk_count"] > 0
    # Only administrators of the document can read job outcomes.
    reader = login(app_client, ingest_db, "niletech/finance-reader")
    assert app_client.get(job_url, headers=auth(reader)).status_code in (403, 404)


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


# -- Docling (M1 parser choice) and its documented fallback -------------------------------

DOCLING_SCAN = {
    "parser": "docling-test",
    "elements": [
        {
            "type": "heading",
            "text": "Delivery note DN-2026-0412",
            "page": 1,
            "heading_path": ["Delivery note DN-2026-0412"],
        },
        {
            "type": "paragraph",
            "text": "Delivered to Alexandria warehouse on 14 April 2026.",
            "page": 1,
            "heading_path": ["Delivery note DN-2026-0412"],
        },
        {
            "type": "table",
            "text": "Item | Qty\nPallet racks | 12",
            "page": 1,
            "heading_path": ["Delivery note DN-2026-0412"],
            "header_end": 10,
        },
    ],
    "notes": {"docling_version": "test", "pages": 1},
}


def version_row(db: DbHarness, document_id: str) -> Any:
    return owner_exec(
        db,
        "SELECT parser, parse_notes, text_sha256, canonical_text, elements, status"
        " FROM document_versions WHERE document_id = :d ORDER BY version DESC LIMIT 1",
        {"d": document_id},
    ).one()


def test_docling_elements_are_stored_and_reindex_never_reparses(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    from app.reporting.model_client import FakeModelClient, GatewayUnavailable

    ingestion = runner(app_client)
    real = ingestion._model
    ingestion._model = FakeModelClient(parse_result=DOCLING_SCAN)
    try:
        document_id = upload(app_client, ingest_db, corpus_doc("delivery-note-scan"))
        assert wait_status(ingest_db, document_id, {"ready", "needs_ocr", "failed"}) == "ready"
    finally:
        ingestion._model = real
    first = version_row(ingest_db, document_id)
    assert first.parser == "docling-test"
    table = first.elements[2]
    assert first.canonical_text[table["start"] : table["end"]] == "Item | Qty\nPallet racks | 12"
    assert table["header_end"] == 10 and table["page"] == 1

    # Docling now unavailable: a reindex rebuilds from the stored elements; the
    # text-layer fallback (which would say needs_ocr for this scan) is never consulted.
    ingestion._model = FakeModelClient(parse_result=GatewayUnavailable("down"))
    try:
        admin = login(app_client, ingest_db, "niletech/access-admin")
        response = app_client.post(f"/api/v1/documents/{document_id}/reindex", headers=auth(admin))
        assert response.status_code == 202
        for _ in range(200):
            if [g[1] for g in generations(ingest_db, document_id)] == ["retired", "active"]:
                break
            time.sleep(0.05)
    finally:
        ingestion._model = real
    assert [g[1] for g in generations(ingest_db, document_id)] == ["retired", "active"]
    again = version_row(ingest_db, document_id)
    assert (again.parser, again.text_sha256, again.elements) == (
        first.parser,
        first.text_sha256,
        first.elements,
    )
    detail = app_client.get(f"/api/v1/documents/{document_id}", headers=auth(admin)).json()
    assert detail["versions"][0]["parser"] == "docling-test"


def test_without_docling_pdfs_use_the_documented_fallback(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    document_id = upload(app_client, ingest_db, corpus_doc("sales-tables-pdf"))
    assert wait_status(ingest_db, document_id, {"ready", "failed"}) == "ready"
    row = version_row(ingest_db, document_id)
    assert row.parser == "pdfplumber-text-v1"
    assert row.parse_notes["docling"] == "unavailable: not_configured"
    assert row.parse_notes["fallback"] == "pdfplumber text layer (M1 documented fallback)"
    assert row.elements and all(
        row.canonical_text[e["start"] : e["end"]].strip() for e in row.elements
    )
    scan = upload(app_client, ingest_db, corpus_doc("delivery-note-scan"))
    assert wait_status(ingest_db, scan, {"needs_ocr", "ready", "failed"}) == "needs_ocr"


def test_a_malformed_docling_result_falls_back_instead_of_failing(
    app_client: TestClient, ingest_db: DbHarness
) -> None:
    from app.reporting.model_client import FakeModelClient

    ingestion = runner(app_client)
    real = ingestion._model
    ingestion._model = FakeModelClient(parse_result={"parser": "other", "elements": "x"})
    try:
        document_id = upload(app_client, ingest_db, corpus_doc("company-overview-pdf"))
        assert wait_status(ingest_db, document_id, {"ready", "failed"}) == "ready"
    finally:
        ingestion._model = real
    row = version_row(ingest_db, document_id)
    assert row.parse_notes["docling"] == "unavailable: malformed_parse_result"
