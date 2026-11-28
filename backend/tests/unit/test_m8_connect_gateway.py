"""M8 connect script: remote reindexing sends only the synthetic fixtures, verified by
identity and CURRENT content (scripts/m8_connect_gateway.py:select_fixtures)."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "m8_connect_gateway.py"
LEAVE = "Annual leave policy 2026"


@pytest.fixture(scope="module")
def connect() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m8_connect_gateway", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["m8_connect_gateway"] = module
    spec.loader.exec_module(module)
    return module


def doc(
    spec: dict[str, Any],
    *,
    document_id: str,
    title: str = LEAVE,
    versions: list[tuple[str, str]] | None = None,
    **over: Any,
) -> dict[str, Any]:
    """An administrator's view of a document; ``versions`` are (filename, sha256)."""

    versions = versions or [(spec["filename"], spec["sha256"])]
    return {
        "document_id": document_id,
        "title": title,
        "department": spec["department"],
        "classification": spec["classification"] or "restricted",
        "permission_state": spec["permission_state"],
        "current_version": len(versions),
        "can_administer": True,
        "versions": [
            {"version": i + 1, "filename": f, "byte_sha256": h} for i, (f, h) in enumerate(versions)
        ],
        **over,
    }


def ids(targets: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    return [d["document_id"] for d, _ in targets]


def test_manifest_hashes_the_actual_fixture_files(connect: ModuleType) -> None:
    manifest = connect.fixture_manifest()
    leave = manifest[LEAVE]
    path = Path(connect.FIXTURES) / "documents" / "files" / leave["filename"]
    assert leave["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (leave["department"], leave["classification"], leave["reason"]) == (
        "hr",
        "internal",
        None,
    )
    draft = manifest["Draft strategy memo"]
    assert draft["reason"] and draft["permission_state"] == "unknown"  # expected not searchable
    assert "Isolation tenant finance handbook" not in manifest  # other tenant


def test_same_title_upload_is_never_sent(connect: ModuleType) -> None:
    manifest = connect.fixture_manifest()
    spec = manifest[LEAVE]
    genuine = doc(spec, document_id="a" * 36)
    impostor = doc(
        spec, document_id="b" * 36, versions=[("leave.md", hashlib.sha256(b"other").hexdigest())]
    )
    same_file_other_bytes = doc(
        spec, document_id="c" * 36, versions=[(spec["filename"], hashlib.sha256(b"x").hexdigest())]
    )
    targets, rejected, missing = connect.select_fixtures(
        [genuine, impostor, same_file_other_bytes], {LEAVE: spec}
    )
    assert ids(targets) == [genuine["document_id"]]
    reasons = dict(rejected)
    assert "is not" in reasons[f"{LEAVE} [bbbbbbbb]"]  # filename differs
    assert "differs from the fixture file" in reasons[f"{LEAVE} [cccccccc]"]
    assert missing == []


def test_replaced_fixture_version_is_not_sent(connect: ModuleType) -> None:
    manifest = connect.fixture_manifest()
    spec = manifest[LEAVE]
    replaced = doc(
        spec,
        document_id="d" * 36,
        versions=[
            (spec["filename"], spec["sha256"]),  # version 1 was the fixture
            (spec["filename"], hashlib.sha256(b"edited policy").hexdigest()),  # now replaced
        ],
    )
    targets, rejected, missing = connect.select_fixtures([replaced], {LEAVE: spec})
    assert targets == []
    assert rejected == [
        (f"{LEAVE} [dddddddd]", "current content (version 2) differs from the fixture file")
    ]
    assert missing == [LEAVE]


def test_ambiguous_wrong_metadata_and_old_backend_are_refused(connect: ModuleType) -> None:
    manifest = connect.fixture_manifest()
    spec = manifest[LEAVE]
    twins = [doc(spec, document_id="e" * 36), doc(spec, document_id="f" * 36)]
    targets, rejected, missing = connect.select_fixtures(twins, {LEAVE: spec})
    assert targets == [] and missing == [LEAVE]
    assert all("ambiguous: 2 documents" in r for _, r in rejected)
    other_dept = doc(spec, document_id="g" * 36, department="finance")
    no_hash = doc(spec, document_id="h" * 36)
    no_hash["versions"][0].pop("byte_sha256")
    not_admin = doc(spec, document_id="i" * 36, can_administer=False)
    for bad, words in (
        (other_dept, "department"),
        (no_hash, "hash is not visible"),
        (not_admin, "not administered"),
    ):
        targets, rejected, _ = connect.select_fixtures([bad], {LEAVE: spec})
        assert targets == [] and words in rejected[0][1]


def test_documents_with_other_titles_are_ignored(connect: ModuleType) -> None:
    manifest = connect.fixture_manifest()
    spec = manifest[LEAVE]
    unrelated = doc(spec, document_id="j" * 36, title="My own upload")
    targets, rejected, missing = connect.select_fixtures([unrelated], {LEAVE: spec})
    assert (targets, rejected, missing) == ([], [], [LEAVE])


def test_fixture_without_permissions_matches_by_state_not_classification(
    connect: ModuleType,
) -> None:
    manifest = connect.fixture_manifest()
    title = "Draft strategy memo"
    spec = manifest[title]
    draft = doc(
        spec, document_id="k" * 36, title=title
    )  # backend shows classification 'restricted'
    targets, rejected, _ = connect.select_fixtures([draft], {title: spec})
    assert ids(targets) == [draft["document_id"]] and rejected == []
    shared = doc(spec, document_id="l" * 36, title=title, permission_state="verified")
    targets, rejected, _ = connect.select_fixtures([shared], {title: spec})
    assert targets == [] and "permission state" in rejected[0][1]


# -- reindex waits for ITS jobs and fails loudly ---------------------------------------------

SPACE = "eaios__baai_bge_m3_5617a9f61b02_1024"


class FakeApi:
    """The documents API as the connect script sees it. Every fixture is genuine; each
    queued job reports ``pending`` for ``delay`` polls, then its scripted final state.
    The document list always says ready/searchable (the OLD generation keeps serving)."""

    def __init__(self, connect: ModuleType, delay: int = 0) -> None:
        self.manifest = connect.fixture_manifest()
        self.docs = []
        for n, (title, spec) in enumerate(self.manifest.items()):
            d = doc(spec, document_id=f"{n:08d}-0000-0000-0000-000000000000", title=title)
            d.update(status="ready", searchable=spec["reason"] is None)
            self.docs.append(d)
        self.delay = delay
        self.post_status: dict[str, int] = {}  # title -> HTTP status for its reindex POST
        self.final: dict[str, dict[str, Any]] = {}  # title -> final job state overrides
        self.polls: dict[str, int] = {}
        self.posted: list[str] = []

    def title_of(self, document_id: str) -> str:
        return str(next(d["title"] for d in self.docs if d["document_id"] == document_id))

    def __call__(
        self, method: str, url: str, token: str | None = None, body: Any = None, timeout: float = 30
    ) -> tuple[int, Any]:
        path = url.split("/api/v1", 1)[1]
        if method == "GET" and path == "/documents":
            return 200, self.docs
        parts = path.strip("/").split("/")
        title = self.title_of(parts[1])
        if method == "POST" and parts[2] == "reindex":
            self.posted.append(title)
            code = self.post_status.get(title, 202)
            return code, ({"job_id": f"job-{parts[1]}"} if code == 202 else None)
        if method == "GET" and parts[2] == "jobs":
            self.polls[title] = self.polls.get(title, 0) + 1
            if self.polls[title] <= self.delay:
                return 200, {"status": "claimed", "outcome": "pending", "in_current_space": False}
            spec = self.manifest[title]
            state = {
                "status": "done",
                "outcome": "needs_ocr"
                if spec["reason"] and spec["permission_state"] == "verified"
                else "active",
                "in_current_space": True,
                "embedding_space": SPACE,
                "chunk_count": 4,
                "error": None,
            }
            state.update(self.final.get(title, {}))
            return 200, state
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def api(connect: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Any:
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(connect, "admin_token", lambda: "admin-token")
    monkeypatch.setattr(connect, "_sleep", sleep)
    monkeypatch.setattr(connect, "_now", lambda: clock["t"])

    def make(delay: int = 0) -> FakeApi:
        fake = FakeApi(connect, delay)
        monkeypatch.setattr(connect, "http", fake)
        fake.sleeps = sleeps  # type: ignore[attr-defined]
        return fake

    return make


def test_reindex_waits_for_its_jobs_while_the_old_index_still_looks_searchable(
    connect: ModuleType, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = api(delay=3)  # a slow worker: three polls of pending per job
    connect.reindex(timeout=600, poll=5)
    assert set(fake.posted) == set(fake.manifest)
    assert all(n == 4 for n in fake.polls.values())  # polled until each job finished
    assert len(fake.sleeps) == 3
    out = capsys.readouterr().out
    assert "13 of 13 expected-searchable synthetic fixtures are searchable" in out


def test_reindex_times_out_nonzero_even_though_documents_look_searchable(
    connect: ModuleType, api: Any
) -> None:
    api(delay=10_000)  # the worker never gets to the jobs; the old index keeps serving
    with pytest.raises(SystemExit) as stopped:
        connect.reindex(timeout=30, poll=5)
    assert stopped.value.code not in (0, None)
    assert "timed out after 30 s with 15 job(s) unfinished" in str(stopped.value)
    assert "job still claimed (timed out)" in str(stopped.value)


def test_http_503_on_a_reindex_request_is_a_failure(connect: ModuleType, api: Any) -> None:
    fake = api()
    fake.post_status[LEAVE] = 503
    with pytest.raises(SystemExit) as stopped:
        connect.reindex(timeout=60, poll=5)
    assert stopped.value.code not in (0, None)
    assert f"reindex request rejected for {LEAVE}: HTTP 503" in str(stopped.value)
    assert LEAVE not in fake.polls  # no job to wait for; the others were still checked
    assert len(fake.polls) == len(fake.manifest) - 1


def test_failed_job_and_wrong_embedding_space_are_failures(connect: ModuleType, api: Any) -> None:
    fake = api()
    fake.final[LEAVE] = {"outcome": "failed", "error": {"code": "ingestion_failed"}}
    fake.final["IT security handbook"] = {"in_current_space": False}
    with pytest.raises(SystemExit) as stopped:
        connect.reindex(timeout=60, poll=5)
    message = str(stopped.value)
    assert f"{LEAVE}: job outcome failed (ingestion_failed)" in message
    assert (
        "IT security handbook: active generation is not in the current embedding space" in message
    )


def test_conflict_409_is_reported_as_a_rejected_request(connect: ModuleType, api: Any) -> None:
    fake = api()
    fake.post_status[LEAVE] = 409
    with pytest.raises(SystemExit) as stopped:
        connect.reindex(timeout=60, poll=5)
    assert f"reindex request rejected for {LEAVE}: changed after the check" in str(stopped.value)
