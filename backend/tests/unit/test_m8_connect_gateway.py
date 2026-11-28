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
