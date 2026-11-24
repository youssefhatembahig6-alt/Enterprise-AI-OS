"""M5 GitHub repository sync and the code pack, on real PostgreSQL, real Qdrant and the
M3 ingestion and answer paths. GitHub is the in-memory double (``m5_support``) and
the model is the labelled fake: integration evidence for permissions, revocation,
deletion and citations, not live GitHub or model-quality evidence."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.conftest import owner_exec
from tests.integration.document_support import auth
from tests.integration.m5_support import M5, REPO, m5
from tests.integration.report_support import Stream, ask

__all__ = ["m5"]

ADMIN = "niletech/access-admin"
FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
CODE_Q = "How long do inventory reservations last before they expire?"
INVENTORY = (
    "\n\n"  # two leading blank lines: line numbers must still match the file
    '"""Inventory reservations for open orders."""\n'
    "\n"
    "RESERVATION_TTL_MINUTES = 45\n"
    "\n"
    "\n"
    "def reserve(sku: str, quantity: int) -> dict[str, int]:\n"
    '    """Reserve stock; inventory reservations expire after RESERVATION_TTL_MINUTES."""\n'
    "    if quantity <= 0:\n"
    '        raise ValueError("quantity must be positive")\n'
    '    return {"quantity": quantity, "ttl": RESERVATION_TTL_MINUTES}\n'
)


def code(m5: M5, ref: str, question: str = CODE_Q) -> Stream:
    return ask(m5.client, m5.token(ref), question, mode="code")


def lines_of(content: str, quote: str) -> tuple[int, int]:
    """1-based source lines of ``quote`` in the file, computed independently."""

    start = content.index(quote)
    end = start + len(quote)
    return content.count("\n", 0, start) + 1, content.count("\n", 0, end - 1) + 1


@pytest.fixture(scope="module")
def repo_source(m5: M5) -> Iterator[dict[str, Any]]:
    repo = m5.hub.repo(REPO, collaborators={"FinReader": "pull", "lead-dev": "admin"})
    repo.files.update(
        {
            "app/inventory.py": INVENTORY.encode(),
            "README.md": b"# Inventory service\n\nReserves stock for orders.\n",
            ".env": b"SECRET=never-ingested\n",
            "node_modules/pkg/index.js": b"module.exports = 1;\n",
            "assets/logo.png": b"\x89PNG\r\n\x1a\nbinary",
            "link.py": b"app/inventory.py",
            "db/huge.sql": b"-- declared larger than the limit\n",
        }
    )
    repo.modes["link.py"] = "120000"
    repo.sizes["db/huge.sql"] = 11 * 1024 * 1024
    repo.submodules.add("third_party/lib")
    assert m5.link(ADMIN, FINANCE, "FinReader").status_code == 200
    source = m5.create_repo_source(ADMIN)
    outcome = m5.sync(ADMIN, source["source_id"])
    assert outcome["status"] == "succeeded", outcome
    for path in ("app/inventory.py", "README.md"):
        m5.wait_ready(m5.item(source["source_id"], path).document_id)
    yield source


def test_sync_records_every_path_and_ingests_only_eligible_files(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    sid = repo_source["source_id"]
    states = {
        path: (row.state, row.reason)
        for path in (
            "app/inventory.py",
            "README.md",
            ".env",
            "node_modules/pkg/index.js",
            "assets/logo.png",
            "link.py",
            "db/huge.sql",
            "third_party/lib",
        )
        if (row := m5.item(sid, path)) is not None
    }
    assert states == {
        "app/inventory.py": ("active", None),
        "README.md": ("active", None),
        ".env": ("excluded", "hidden_file"),
        "node_modules/pkg/index.js": ("excluded", "dependency_or_build_folder"),
        "assets/logo.png": ("unsupported", "file_type"),
        "link.py": ("symlink", "symlink_not_followed"),
        "db/huge.sql": ("too_large", "over_10_mb"),
        "third_party/lib": ("unsupported", "submodule"),
    }
    # Only the two eligible blobs were downloaded; the oversized one never was.
    assert m5.hub.downloads() == 2
    view = m5.client.get(f"/api/v1/admin/sources/{sid}", headers=auth(m5.token(ADMIN))).json()
    assert view["access"] == "current" and view["visibility"] == "private"
    assert view["external_revision"] == m5.hub.head()
    assert view["unresolved_principals"] == [
        {"type": "github_user", "login": "lead-dev", "reason": "no_linked_user"}
    ]


def test_code_answer_cites_repository_file_revision_and_exact_lines(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    stream = code(m5, FINANCE)
    assert stream.done["outcome"] == "published", stream.raw[-800:]
    assert stream.done["verification"]["code_execution"] == "none"
    (citation,) = [f.data for f in stream.of("citation")]
    assert citation["repository"] == REPO
    assert citation["path"] == "app/inventory.py"
    assert citation["commit"] == m5.hub.head()
    first, last = lines_of(INVENTORY, citation["quote"])
    assert (citation["line_start"], citation["line_end"]) == (first, last)
    assert citation["url"] == (
        f"https://github.com/{REPO}/blob/{m5.hub.head()}/app/inventory.py#L{first}-L{last}"
    )
    opened = m5.client.get(
        f"/api/v1/runs/{stream.run_id}/citations/{citation['citation_id']}",
        headers=auth(m5.token(FINANCE)),
    ).json()
    assert opened["integrity"] == "verified"
    assert (opened["line_start"], opened["line_end"], opened["commit"]) == (
        first,
        last,
        m5.hub.head(),
    )


def test_users_who_are_not_linked_collaborators_get_no_sources(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    assert code(m5, SALES).done["abstention"]["code"] == "no_sources"
    assert m5.link(ADMIN, SALES, "outsider").status_code == 200  # not a collaborator
    try:
        denied = code(m5, SALES)
        assert denied.done["abstention"]["code"] == "no_sources"
        assert "RESERVATION_TTL" not in denied.raw
    finally:
        m5.link(ADMIN, SALES, None)


def test_unlinking_a_login_revokes_at_once_without_a_sync(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    before = m5.epoch()
    assert m5.link(ADMIN, FINANCE, None).status_code == 200
    assert m5.epoch() > before
    try:
        assert code(m5, FINANCE).done["abstention"]["code"] == "no_sources"
    finally:
        assert m5.link(ADMIN, FINANCE, "finreader").status_code == 200
    assert code(m5, FINANCE).done["outcome"] == "published"


def test_collaborator_removed_in_github_loses_access_at_the_next_sync(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    repo = m5.hub.repos[REPO]
    repo.collaborators.pop("FinReader")
    try:
        # Until the next verified sync the last verified principals apply (ADR 0502).
        assert code(m5, FINANCE).done["outcome"] == "published"
        assert m5.sync(ADMIN, repo_source["source_id"])["status"] == "succeeded"
        denied = code(m5, FINANCE)
        assert denied.done["abstention"]["code"] == "no_sources"
    finally:
        repo.collaborators["FinReader"] = "pull"
        assert m5.sync(ADMIN, repo_source["source_id"])["status"] == "succeeded"
    assert code(m5, FINANCE).done["outcome"] == "published"


@pytest.mark.parametrize(
    ("break_it", "code_expected"),
    [
        ("inaccessible", "repository_not_accessible"),
        ("collaborators", "collaborators_unavailable"),
        ("renamed", "repository_moved"),
    ],
)
def test_lost_repository_access_fails_closed(
    m5: M5, repo_source: dict[str, Any], break_it: str, code_expected: str
) -> None:
    sid = repo_source["source_id"]
    key = REPO.lower()
    if break_it == "inaccessible":
        m5.hub.inaccessible.add(key)
    elif break_it == "collaborators":
        m5.hub.collaborators_forbidden.add(key)
    else:
        m5.hub.renamed[key] = "niletech/renamed-service"
    try:
        outcome = m5.sync(ADMIN, sid)
        assert (outcome["status"], outcome["error_code"]) == ("failed", code_expected)
        view = m5.client.get(f"/api/v1/admin/sources/{sid}", headers=auth(m5.token(ADMIN)))
        assert view.json()["access"] == "last_sync_failed"
        denied = code(m5, FINANCE)
        assert denied.done["abstention"]["code"] == "no_sources"
    finally:
        m5.hub.inaccessible.discard(key)
        m5.hub.collaborators_forbidden.discard(key)
        m5.hub.renamed.pop(key, None)
        assert m5.sync(ADMIN, sid)["status"] == "succeeded"
    assert code(m5, FINANCE).done["outcome"] == "published"


def test_public_visibility_never_widens_access(m5: M5, repo_source: dict[str, Any]) -> None:
    repo = m5.hub.repos[REPO]
    repo.visibility = "public"
    try:
        assert m5.sync(ADMIN, repo_source["source_id"])["status"] == "succeeded"
        assert code(m5, SALES).done["abstention"]["code"] == "no_sources"
        audited = owner_exec(
            m5.db,
            "SELECT details FROM audit_events WHERE event_type = 'connector.visibility'"
            " ORDER BY occurred_at DESC LIMIT 1",
        ).scalar_one()
        assert (audited["from"], audited["to"]) == ("private", "public")
    finally:
        repo.visibility = "private"
        assert m5.sync(ADMIN, repo_source["source_id"])["status"] == "succeeded"


def test_allowlist_removal_fails_before_any_github_request(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    m5.set_allowed(["niletech/other-service"])
    m5.hub.calls.clear()
    try:
        outcome = m5.sync(ADMIN, repo_source["source_id"])
        assert (outcome["status"], outcome["error_code"]) == ("failed", "repository_not_allowed")
        assert m5.hub.calls == []  # never reached GitHub
        assert code(m5, FINANCE).done["abstention"]["code"] == "no_sources"
    finally:
        m5.set_allowed([REPO, "niletech/other-service", "niletech/renamed-service"])
        assert m5.sync(ADMIN, repo_source["source_id"])["status"] == "succeeded"


def test_edit_rename_and_delete_follow_the_repository(m5: M5, repo_source: dict[str, Any]) -> None:
    sid = repo_source["source_id"]
    old_commit = m5.hub.head()
    before = code(m5, FINANCE)
    (old_citation,) = [f.data for f in before.of("citation")]
    repo = m5.hub.repos[REPO]
    edited = INVENTORY.replace("= 45", "= 30")
    repo.files["app/inventory.py"] = edited.encode()
    repo.files["docs/GUIDE.md"] = repo.files.pop("README.md")  # rename
    m5.hub.commit()
    outcome = m5.sync(ADMIN, sid)
    assert outcome["status"] == "succeeded", outcome
    assert outcome["stats"]["updated"] == 1 and outcome["stats"]["created"] == 1
    doc = m5.item(sid, "app/inventory.py").document_id
    m5.wait_ready(doc, version=2)
    readme = m5.item(sid, "README.md")
    assert readme.state == "removed"
    deleted = owner_exec(
        m5.db, "SELECT deleted_at FROM documents WHERE id = :d", {"d": readme.document_id}
    ).scalar_one()
    assert deleted is not None
    m5.wait_ready(m5.item(sid, "docs/GUIDE.md").document_id)
    after = code(m5, FINANCE, "What is the reservation TTL in minutes?")
    assert after.done["outcome"] == "published", after.raw[-600:]
    (new_citation,) = [f.data for f in after.of("citation")]
    assert new_citation["commit"] == m5.hub.head() != old_commit
    # The earlier answer still opens the version and commit it cited.
    reopened = m5.client.get(
        f"/api/v1/runs/{before.run_id}/citations/{old_citation['citation_id']}",
        headers=auth(m5.token(FINANCE)),
    ).json()
    assert reopened["commit"] == old_commit and reopened["version"] == 1
    assert reopened["is_current_version"] is False


def test_documents_pack_never_searches_repository_code(m5: M5, repo_source: dict[str, Any]) -> None:
    stream = ask(m5.client, m5.token(FINANCE), CODE_Q, mode="documents")
    assert stream.done["abstention"]["code"] == "no_sources"


def test_disabled_github_connector_denies_repository_content(
    m5: M5, repo_source: dict[str, Any]
) -> None:
    tenant = m5.db.foundation.tenants["niletech"].id
    sql = "UPDATE tenant_settings SET enabled_connectors = :c WHERE tenant_id = :t"
    owner_exec(m5.db, sql, {"c": ["upload", "local_folder"], "t": tenant})
    try:
        assert code(m5, FINANCE).done["abstention"]["code"] == "no_sources"
    finally:
        owner_exec(
            m5.db, sql, {"c": ["upload", "google_drive", "github", "local_folder"], "t": tenant}
        )
    assert code(m5, FINANCE).done["outcome"] == "published"


def test_deleting_a_repository_source_tombstones_its_documents(m5: M5) -> None:
    other = m5.hub.repo("niletech/other-service", collaborators={"finreader": "pull"})
    other.files["svc/main.py"] = b"def handler():\n    return 'ok'\n"
    source = m5.create_repo_source(ADMIN, "niletech/other-service")
    assert m5.sync(ADMIN, source["source_id"])["status"] == "succeeded"
    doc = m5.item(source["source_id"], "svc/main.py").document_id
    m5.wait_ready(doc)
    response = m5.client.patch(
        f"/api/v1/admin/sources/{source['source_id']}",
        json={"status": "deleted"},
        headers=auth(m5.token(ADMIN)),
    )
    assert response.status_code == 200, response.text
    row = owner_exec(
        m5.db,
        "SELECT d.deleted_at, r.status FROM documents d JOIN resources r ON r.id = d.id"
        " WHERE d.id = :d",
        {"d": doc},
    ).one()
    assert row.deleted_at is not None and row.status == "deleted"
    assert m5.item(source["source_id"], "svc/main.py").state == "removed"


def test_repository_sources_need_the_operator_allowlist_and_connector(m5: M5) -> None:
    body = {
        "kind": "github",
        "name": "x",
        "external_root": "someone/elsewhere",
        "department": "it",
        "classification": "internal",
    }
    response = m5.client.post("/api/v1/admin/sources", json=body, headers=auth(m5.token(ADMIN)))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "repository_not_allowed"


def test_linking_needs_tenant_wide_identity_admin(m5: M5) -> None:
    scoped = m5.link("niletech/finance-admin", SALES, "someone")
    assert scoped.status_code == 403
    self_link = m5.link(ADMIN, ADMIN, "admin-login")
    assert self_link.status_code == 422
    assert self_link.json()["error"]["code"] == "cannot_link_self"
