"""M5 desktop local-folder connector, backend half, on real PostgreSQL and Qdrant through
the real API. These tests send the events the desktop app sends; the desktop watcher
itself is tested in ``apps/desktop`` (Node tests on Debian and Windows)."""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Iterator
from itertools import count
from typing import Any

import pytest

from tests.integration.conftest import owner_exec
from tests.integration.document_support import auth
from tests.integration.m5_support import M5, m5
from tests.integration.report_support import ask

__all__ = ["m5"]

FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
ADMIN = "niletech/access-admin"
NOTE = b"# Expense notes\n\nTaxi receipts above 25 USD need a scanned copy.\n"
NOTE_Q = "Which taxi receipts need a scanned copy?"
_seq = count(1)


def upsert(path: str, data: bytes, *, seq: int | None = None, **over: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "seq": seq if seq is not None else next(_seq),
        "op": "upsert",
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "content_base64": base64.b64encode(data).decode(),
        **over,
    }


def delete(path: str, *, seq: int | None = None) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "seq": seq if seq is not None else next(_seq),
        "op": "delete",
        "path": path,
    }


def send(m5: M5, ref: str, source_id: str, *events: dict[str, Any]) -> Any:
    return m5.client.post(
        f"/api/v1/local-folders/{source_id}/events",
        json={"events": list(events)},
        headers=auth(m5.token(ref)),
    )


def results(m5: M5, ref: str, source_id: str, *events: dict[str, Any]) -> list[dict[str, Any]]:
    response = send(m5, ref, source_id, *events)
    assert response.status_code == 200, response.text
    return list(response.json()["results"])


def register(m5: M5, ref: str, folder_id: str | None = None, name: str = "Notes") -> Any:
    return m5.client.post(
        "/api/v1/local-folders",
        json={"folder_id": folder_id or str(uuid.uuid4()), "name": name, "device": "laptop"},
        headers=auth(m5.token(ref)),
    )


def answer(m5: M5, ref: str, question: str = NOTE_Q) -> Any:
    return ask(m5.client, m5.token(ref), question, mode="documents")


@pytest.fixture(scope="module")
def folder(m5: M5) -> Iterator[dict[str, Any]]:
    response = register(m5, FINANCE)
    assert response.status_code == 201, response.text
    source = dict(response.json())
    (result,) = results(m5, FINANCE, source["source_id"], upsert("expenses/notes.md", NOTE))
    assert result["result"] == "created", result
    m5.wait_ready(result["document_id"])
    yield source


def test_backend_assigns_ownership_and_only_the_owner_reads(m5: M5, folder: dict[str, Any]) -> None:
    assert (folder["department"], folder["classification"]) == ("finance", "internal")
    assert folder["shared_groups"] == [] and folder["access"] == "current"
    owned = answer(m5, FINANCE)
    assert owned.done["outcome"] == "published", owned.raw[-600:]
    assert "25 USD" in next(f.data for f in owned.of("citation"))["quote"]
    assert answer(m5, SALES).done["abstention"]["code"] == "no_sources"
    # No freshness bound applies: nothing external can go stale for a local folder.
    row = owner_exec(
        m5.db,
        "SELECT last_verified_sync_at, owner_user_id FROM connector_sources WHERE id = :s",
        {"s": folder["source_id"]},
    ).one()
    assert row.last_verified_sync_at is None and row.owner_user_id == m5.user_id(FINANCE)


def test_registration_is_idempotent_and_owned(m5: M5, folder: dict[str, Any]) -> None:
    again = register(m5, FINANCE, folder["folder_id"])
    assert again.status_code == 201 and again.json()["source_id"] == folder["source_id"]
    stolen = register(m5, SALES, folder["folder_id"])
    assert stolen.status_code == 422 and stolen.json()["error"]["code"] == "invalid_source"
    # Another user cannot push events into, list or read the folder: not found.
    assert send(m5, SALES, folder["source_id"], upsert("x.md", b"# x\n")).status_code == 404
    items = m5.client.get(
        f"/api/v1/local-folders/{folder['source_id']}/items", headers=auth(m5.token(SALES))
    )
    assert items.status_code == 404


def test_duplicate_events_and_unchanged_content_change_nothing(
    m5: M5, folder: dict[str, Any]
) -> None:
    sid = folder["source_id"]
    event = upsert("expenses/notes.md", NOTE)
    (first,) = results(m5, FINANCE, sid, event)
    assert first["result"] == "unchanged"  # same SHA-256 as the current version
    (replay,) = results(m5, FINANCE, sid, event)  # same event id: an offline retry
    assert (replay["result"], replay["first_result"]) == ("duplicate", "unchanged")
    versions = owner_exec(
        m5.db,
        "SELECT count(*) FROM document_versions v JOIN connector_items i"
        " ON i.document_id = v.document_id WHERE i.source_id = :s AND i.path = :p",
        {"s": sid, "p": "expenses/notes.md"},
    ).scalar_one()
    assert versions == 1


def test_a_late_retry_cannot_resurrect_a_deleted_file(m5: M5, folder: dict[str, Any]) -> None:
    sid = folder["source_id"]
    data = b"# Draft\n\nTemporary draft text about procurement.\n"
    created = next(_seq)
    removed = next(_seq)
    (made,) = results(m5, FINANCE, sid, upsert("draft.md", data, seq=created))
    assert made["result"] == "created"
    (gone,) = results(m5, FINANCE, sid, delete("draft.md", seq=removed))
    assert gone["result"] == "removed"
    # The upsert was retried after the delete (offline queue): older sequence, ignored.
    (late,) = results(m5, FINANCE, sid, upsert("draft.md", data, seq=created))
    assert late["result"] == "stale"
    row = m5.item(sid, "draft.md")
    assert row.state == "removed"
    deleted = owner_exec(
        m5.db, "SELECT deleted_at FROM documents WHERE id = :d", {"d": made["document_id"]}
    ).scalar_one()
    assert deleted is not None


def test_rules_are_reapplied_on_the_server(m5: M5, folder: dict[str, Any]) -> None:
    sid = folder["source_id"]
    big = upsert("big.md", b"x")
    big.update(size=11 * 1024 * 1024, content_base64=None, sha256="0" * 64)
    out = results(
        m5,
        FINANCE,
        sid,
        upsert(".secret/keys.md", b"# keys\n"),
        upsert("~$report.md", b"# lock\n"),
        upsert("node_modules/a/readme.md", b"# dep\n"),
        upsert("tool.exe", b"MZ\x90\x00"),
        big,
        upsert("tampered.md", b"# real\n", sha256=hashlib.sha256(b"other").hexdigest()),
        upsert("../escape.md", b"# out\n"),
        upsert("/etc/passwd.md", b"# abs\n"),
        upsert("dir\\win.md", b"# win\n"),
    )
    got = [(r["result"], r.get("reason")) for r in out]
    assert got == [
        ("excluded", "hidden_folder"),
        ("excluded", "temporary_or_generated_file"),
        ("excluded", "dependency_or_build_folder"),
        ("unsupported", "file_type"),
        ("too_large", "over_10_mb"),
        ("unsupported", "checksum_mismatch"),
        ("rejected", "invalid_path:traversal"),
        ("rejected", "invalid_path:absolute"),
        ("rejected", "invalid_path:bad_characters"),
    ]
    created = owner_exec(
        m5.db,
        "SELECT count(*) FROM connector_items WHERE source_id = :s AND document_id IS NOT NULL"
        " AND path IN ('.secret/keys.md', '~$report.md', 'tool.exe', 'big.md', 'tampered.md')",
        {"s": sid},
    ).scalar_one()
    assert created == 0


def test_sharing_is_backend_assigned_and_revocable(m5: M5, folder: dict[str, Any]) -> None:
    sid = folder["source_id"]

    def share(ref: str, source_id: str, groups: list[str]) -> Any:
        return m5.client.put(
            f"/api/v1/local-folders/{source_id}/sharing",
            json={"groups": groups},
            headers=auth(m5.token(ref)),
        )

    before = m5.epoch()
    shared = share(FINANCE, sid, ["all-staff"])
    assert shared.status_code == 200 and shared.json()["shared_groups"] == ["all-staff"]
    assert m5.epoch() > before
    assert answer(m5, SALES).done["outcome"] == "published"
    assert share(FINANCE, sid, []).status_code == 200
    assert answer(m5, SALES).done["abstention"]["code"] == "no_sources"  # immediate
    # Not the owner: not found. A group the owner is not in: refused.
    assert share(SALES, sid, ["all-staff"]).status_code == 404
    sales_folder = register(m5, SALES, name="Sales notes").json()
    bad = share(SALES, sales_folder["source_id"], ["finance-team"])
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_group"


def test_delete_event_tombstones_and_answers_stop(m5: M5, folder: dict[str, Any]) -> None:
    sid = folder["source_id"]
    data = b"# Parking\n\nVisitor parking passes are issued at the front desk.\n"
    (made,) = results(m5, FINANCE, sid, upsert("parking.md", data))
    m5.wait_ready(made["document_id"])
    question = "Where are visitor parking passes issued?"
    assert answer(m5, FINANCE, question).done["outcome"] == "published"
    (gone,) = results(m5, FINANCE, sid, delete("parking.md"))
    assert gone["result"] == "removed"
    assert answer(m5, FINANCE, question).done["abstention"]["code"] == "no_sources"
    (again,) = results(m5, FINANCE, sid, delete("parking.md"))
    assert again["result"] == "noop"


def test_disabled_connector_refuses_registration_and_denies_content(
    m5: M5, folder: dict[str, Any]
) -> None:
    tenant = m5.db.foundation.tenants["niletech"].id
    sql = "UPDATE tenant_settings SET enabled_connectors = :c WHERE tenant_id = :t"
    owner_exec(m5.db, sql, {"c": ["upload", "google_drive", "github"], "t": tenant})
    try:
        refused = register(m5, FINANCE)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "capability_disabled"
        assert answer(m5, FINANCE).done["abstention"]["code"] == "no_sources"
    finally:
        owner_exec(
            m5.db, sql, {"c": ["upload", "google_drive", "github", "local_folder"], "t": tenant}
        )
    assert answer(m5, FINANCE).done["outcome"] == "published"


def test_admin_sees_local_folders_but_cannot_change_their_attributes(
    m5: M5, folder: dict[str, Any]
) -> None:
    token = m5.token(ADMIN)
    listed = m5.client.get("/api/v1/admin/sources", headers=auth(token)).json()
    mine = next(s for s in listed if s["source_id"] == folder["source_id"])
    assert mine["kind"] == "local_folder" and mine["owner"].startswith("finance.reader@")
    patch = m5.client.patch(
        f"/api/v1/admin/sources/{folder['source_id']}",
        json={"classification": "public"},
        headers=auth(token),
    )
    assert patch.status_code == 422 and patch.json()["error"]["code"] == "managed_by_owner"
    sync = m5.client.post(f"/api/v1/admin/sources/{folder['source_id']}/sync", headers=auth(token))
    assert sync.status_code == 422


def test_owner_deleting_the_folder_tombstones_everything(m5: M5) -> None:
    source = register(m5, FINANCE, name="Temporary").json()
    (made,) = results(
        m5, FINANCE, source["source_id"], upsert("a.md", b"# A\n\nAlpha bravo charlie.\n")
    )
    m5.wait_ready(made["document_id"])
    response = m5.client.delete(
        f"/api/v1/local-folders/{source['source_id']}", headers=auth(m5.token(FINANCE))
    )
    assert response.status_code == 204
    row = owner_exec(
        m5.db,
        "SELECT d.deleted_at, cs.status FROM documents d JOIN resources r ON r.id = d.id"
        " JOIN connector_sources cs ON cs.id = r.source_id WHERE d.id = :d",
        {"d": made["document_id"]},
    ).one()
    assert row.deleted_at is not None and row.status == "deleted"
    assert send(m5, FINANCE, source["source_id"], upsert("b.md", b"# B\n")).status_code == 404
