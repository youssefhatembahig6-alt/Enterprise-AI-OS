"""M4 Google Drive sync on real PostgreSQL, real Qdrant and the M3 ingestion and answer
paths. Drive itself is the in-memory double (``drive_support``): this is integration
evidence for the sync logic and the fail-closed rules, not live Drive evidence."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.connectors.drive import DrivePermission
from app.db.engine import tenant_transaction
from tests.integration.conftest import owner_exec
from tests.integration.document_support import auth
from tests.integration.m4_support import FINANCE_GROUP_EMAIL, M4, TRAVEL, TRAVEL_Q, m4
from tests.integration.report_support import ask, replay

__all__ = ["m4"]  # module fixture

FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
ADMIN = "niletech/access-admin"


def setup_source(m4: M4, **kw: Any) -> tuple[Any, str, str, str]:
    """A fresh Drive folder with the travel policy shared with the finance reader."""

    drive = m4.new_drive()
    file_id = drive.file("travel-policy.md", TRAVEL, users=(m4.user_email(FINANCE),))
    admin = m4.token(ADMIN)
    source = m4.create_source(admin, drive.root_id, **kw)
    outcome = m4.sync(admin, source["source_id"])
    assert outcome["status"] == "succeeded", outcome
    doc = m4.document_for(source["source_id"], file_id)
    assert doc is not None
    m4.wait_ready(doc)
    return drive, source["source_id"], file_id, doc


def answer(m4: M4, ref: str, question: str = TRAVEL_Q) -> Any:
    return ask(m4.client, m4.token(ref), question, mode="documents")


# -- first import, authorized answer, refusal ---------------------------------------------


def test_first_import_answers_with_a_drive_citation(m4: M4) -> None:
    _, source_id, _, doc = setup_source(m4)
    stream = answer(m4, FINANCE)
    assert stream.done["outcome"] == "published", stream.raw[-600:]
    (citation,) = [f.data for f in stream.of("citation")]
    assert citation["document_id"] == doc
    assert "60 USD" in citation["quote"]
    opened = m4.client.get(
        f"/api/v1/runs/{stream.run_id}/citations/{citation['citation_id']}",
        headers=auth(m4.token(FINANCE)),
    )
    assert opened.status_code == 200 and opened.json()["integrity"] == "verified"
    # Not shared with the sales reader in Drive: the same no-sources refusal as M3.
    denied = answer(m4, SALES)
    assert denied.done["abstention"]["code"] == "no_sources"
    assert "60 USD" not in denied.raw
    row = owner_exec(
        m4.db,
        "SELECT d.source, r.source_id, r.permission_state FROM documents d"
        " JOIN resources r ON r.id = d.id WHERE d.id = :d",
        {"d": doc},
    ).one()
    assert (row.source, str(row.source_id), row.permission_state) == (
        "google_drive",
        source_id,
        "verified",
    )
    view = m4.source(m4.token(ADMIN), source_id)
    assert view["access"] == "current" and view["last_verified_sync_at"]
    assert view["recent_runs"][0]["stats"]["created"] == 1


def doc_grants(m4: M4, document_id: str | None) -> list[str]:
    docs = m4.client.get("/api/v1/admin/documents", headers=auth(m4.token(ADMIN))).json()
    return next(d["grants"] for d in docs if d["document_id"] == document_id)


def test_unlinked_groups_and_link_sharing_grant_nobody(m4: M4) -> None:
    drive = m4.new_drive()
    unlinked = drive.file(
        "unlinked.md",
        "# Unlinked\n\nUnlinked group text.\n",
        groups=("everyone@drive.example",),
        extra=(
            DrivePermission("anyone", "reader"),
            DrivePermission("domain", "reader", None, "x.example"),
        ),
    )
    drive.group_members["everyone@drive.example"] = {m4.user_email(FINANCE)}
    admin = m4.token(ADMIN)
    source = m4.create_source(admin, drive.root_id)
    m4.sync(admin, source["source_id"])
    assert doc_grants(m4, m4.document_for(source["source_id"], unlinked)) == []
    items = {i["external_id"]: i for i in m4.source(admin, source["source_id"])["items"]}
    reasons = sorted(p["reason"] for p in items[unlinked]["unresolved_principals"])
    # Verified Google members do not help without a link; the owner has no tenant account.
    assert reasons == ["no_group_link", "no_tenant_user", "not_mapped", "not_mapped"]


def test_group_access_needs_verified_current_google_membership(m4: M4) -> None:
    """A link to an internal group never establishes who is in the Google group. The
    finance reader stays in the internal finance-team group throughout."""

    drive = m4.new_drive()
    file_id = drive.file(
        "group-policy.md",
        "# Group policy\n\nThe group policy marker phrase is amber falcon.\n",
        groups=(FINANCE_GROUP_EMAIL,),
        users=(m4.user_email(SALES),),  # an independent direct share
    )
    admin = m4.token(ADMIN)
    source_id = m4.create_source(admin, drive.root_id)["source_id"]
    question = "What is the group policy marker phrase?"
    sales_grant = f"allow:user:{m4.user_email(SALES)}"
    finance_grant = f"allow:user:{m4.user_email(FINANCE)}"

    def in_internal_group() -> bool:
        tenant = m4.db.foundation.tenants["niletech"]
        return bool(
            owner_exec(
                m4.db,
                "SELECT 1 FROM group_memberships WHERE group_id = :g AND user_id = :u",
                {"g": tenant.groups["finance-team"].id, "u": tenant.users["finance-reader"].id},
            ).scalar()
        )

    # 1. Membership unknown (no directory access): the group grants nobody.
    assert m4.sync(admin, source_id)["status"] == "succeeded"
    doc = m4.document_for(source_id, file_id)
    m4.wait_ready(str(doc))
    assert doc_grants(m4, doc) == [sales_grant]  # the direct share is preserved
    items = m4.source(admin, source_id)["items"]
    assert {
        "type": "group",
        "email": FINANCE_GROUP_EMAIL,
        "reason": "group_membership_unverified",
    } in items[0]["unresolved_principals"]
    assert in_internal_group()
    assert answer(m4, FINANCE, question).done["abstention"]["code"] == "no_sources"
    assert answer(m4, SALES, question).done["outcome"] == "published"

    # 2. Membership verified and the reader is a member: access through the group.
    drive.group_members[FINANCE_GROUP_EMAIL] = {m4.user_email(FINANCE)}
    m4.sync(admin, source_id)
    assert doc_grants(m4, doc) == sorted([finance_grant, sales_grant])
    assert answer(m4, FINANCE, question).done["outcome"] == "published"

    # 3. Google membership revoked; still in the internal group: denied.
    drive.group_members[FINANCE_GROUP_EMAIL] = set()
    assert m4.sync(admin, source_id)["status"] == "succeeded"
    assert in_internal_group()
    assert doc_grants(m4, doc) == [sales_grant]
    assert answer(m4, FINANCE, question).done["abstention"]["code"] == "no_sources"

    # 4. Membership unknown again: another successful sync does not restore access.
    del drive.group_members[FINANCE_GROUP_EMAIL]
    assert m4.sync(admin, source_id)["status"] == "succeeded"
    assert doc_grants(m4, doc) == [sales_grant]
    assert answer(m4, FINANCE, question).done["abstention"]["code"] == "no_sources"
    assert answer(m4, SALES, question).done["outcome"] == "published"


# -- incremental updates -------------------------------------------------------------------


def test_unchanged_sync_downloads_nothing_and_keeps_the_policy_epoch(m4: M4) -> None:
    drive, source_id, _, doc = setup_source(m4)
    admin = m4.token(ADMIN)
    downloads, epoch = drive.downloads(), m4.epoch()
    outcome = m4.sync(admin, source_id)
    assert outcome["status"] == "succeeded"
    assert drive.downloads() == downloads
    assert outcome["stats"].get("created", 0) == outcome["stats"].get("updated", 0) == 0
    assert outcome["stats"].get("permissions_changed", 0) == 0
    assert m4.epoch() == epoch
    assert (
        owner_exec(
            m4.db, "SELECT current_version FROM documents WHERE id = :d", {"d": doc}
        ).scalar_one()
        == 1
    )


def test_content_change_creates_a_new_version(m4: M4) -> None:
    drive, source_id, file_id, doc = setup_source(m4)
    drive.edit(file_id, TRAVEL.replace("60 USD", "75 USD"))
    outcome = m4.sync(m4.token(ADMIN), source_id)
    assert outcome["stats"]["updated"] == 1
    m4.wait_ready(doc, version=2)
    stream = answer(m4, FINANCE)
    assert stream.done["outcome"] == "published", stream.raw[-500:]
    citation = stream.of("citation")[0].data
    assert "75 USD" in citation["quote"] and citation["version"] == 2


# -- sharing changes ------------------------------------------------------------------------


def test_revoked_sharing_applies_after_the_next_sync(m4: M4) -> None:
    drive, source_id, file_id, doc = setup_source(m4)
    token = m4.token(FINANCE)
    first_sync = m4.source(m4.token(ADMIN), source_id)
    published = ask(m4.client, token, TRAVEL_Q, mode="documents")
    cid = published.of("citation")[0].data["citation_id"]
    drive.unshare(file_id, m4.user_email(FINANCE))
    # Before the sync the application still has the last verified permissions; the
    # admin view shows when they were verified (no promise of instant external revocation).
    assert answer(m4, FINANCE).done["outcome"] == "published"
    outcome = m4.sync(m4.token(ADMIN), source_id)
    assert outcome["stats"]["permissions_changed"] == 1
    after = ask(m4.client, token, TRAVEL_Q, mode="documents")
    assert after.done["abstention"]["code"] == "no_sources"
    assert "60 USD" not in after.raw
    reopen = m4.client.get(f"/api/v1/runs/{published.run_id}/citations/{cid}", headers=auth(token))
    assert reopen.status_code == 404
    again = replay(m4.client, token, str(published.run_id))
    assert again.of("error")[-1].data["code"] == "access_revoked"
    assert "60 USD" not in again.raw
    # Sharing again restores access at the next sync.
    second_sync = m4.source(m4.token(ADMIN), source_id)
    if target := os.environ.get("EAIOS_M4_EVIDENCE"):
        Path(target).write_text(
            json.dumps(
                {
                    "drive": "in-memory test double (tests/integration/drive_support.py)",
                    "inference": published.done["inference"],
                    "sync_1": {k: first_sync[k] for k in ("last_verified_sync_at", "access")}
                    | {"run": first_sync["recent_runs"][0]},
                    "answer_before": {
                        "outcome": published.done["outcome"],
                        "citation": published.of("citation")[0].data,
                    },
                    "citation_before_status": 200,
                    "sync_2": {k: second_sync[k] for k in ("last_verified_sync_at", "access")}
                    | {"run": second_sync["recent_runs"][0]},
                    "answer_after": {
                        "outcome": after.done["outcome"],
                        "abstention": after.done["abstention"],
                    },
                    "citation_after_status": reopen.status_code,
                    "replay_after_errors": [f.data for f in again.of("error")],
                },
                indent=2,
                sort_keys=True,
            )
        )
    drive.share(file_id, m4.user_email(FINANCE))
    m4.sync(m4.token(ADMIN), source_id)
    assert answer(m4, FINANCE).done["outcome"] == "published"
    assert doc  # same document identity throughout


# -- moves and deletions ------------------------------------------------------------------


def test_rename_and_move_inside_the_folder_keep_the_document(m4: M4) -> None:
    drive, source_id, file_id, doc = setup_source(m4)
    sub = drive.folder("Policies")
    drive.move(file_id, sub)
    drive.rename(file_id, "travel-policy-2026.md")
    outcome = m4.sync(m4.token(ADMIN), source_id)
    assert outcome["stats"]["moved_or_renamed"] == 1
    assert m4.document_for(source_id, file_id) == doc
    title = owner_exec(m4.db, "SELECT title FROM documents WHERE id = :d", {"d": doc}).scalar_one()
    assert title == "travel-policy-2026"
    items = m4.source(m4.token(ADMIN), source_id)["items"]
    assert items[0]["path"] == "/Policies/travel-policy-2026.md"
    assert answer(m4, FINANCE).done["outcome"] == "published"


def test_moved_out_trashed_and_deleted_files_are_tombstoned(m4: M4) -> None:
    drive = m4.new_drive()
    elsewhere = drive.folder("Elsewhere")
    drive.nodes[elsewhere].parent = None  # a folder outside the synced tree
    reader = (m4.user_email(FINANCE),)
    moved = drive.file("moved.md", "# Moved\n\nMoved out text.\n", users=reader)
    trashed = drive.file("trashed.md", "# Trashed\n\nTrashed text.\n", users=reader)
    deleted = drive.file("deleted.md", "# Deleted\n\nDeleted text.\n", users=reader)
    kept = drive.file("kept.md", "# Kept\n\nKept text.\n", users=reader)
    admin = m4.token(ADMIN)
    source = m4.create_source(admin, drive.root_id)
    sid = source["source_id"]
    m4.sync(admin, sid)
    docs = {f: m4.document_for(sid, f) for f in (moved, trashed, deleted, kept)}
    drive.move(moved, elsewhere)
    drive.trash(trashed)
    drive.delete(deleted)
    outcome = m4.sync(admin, sid)
    assert outcome["stats"]["removed"] == 3
    rows = {
        str(r.id): r
        for r in owner_exec(
            m4.db,
            "SELECT d.id, d.deleted_at, r.status FROM documents d JOIN resources r ON r.id = d.id"
            " WHERE d.id = ANY(:ids)",
            {"ids": [uuid.UUID(d) for d in docs.values() if d]},
        ).all()
    }
    for f in (moved, trashed, deleted):
        row = rows[docs[f]]  # type: ignore[index]
        assert row.deleted_at is not None and row.status == "deleted"
    assert rows[docs[kept]].deleted_at is None  # type: ignore[index]
    # A file moved back gets a new document; the tombstone is never resurrected.
    drive.move(moved, drive.root_id)
    m4.sync(admin, sid)
    assert m4.document_for(sid, moved) not in (None, docs[moved])


# -- fail closed -----------------------------------------------------------------------------


def test_failed_sync_denies_the_whole_source_until_a_good_sync(m4: M4) -> None:
    drive, source_id, _, _ = setup_source(m4)
    admin = m4.token(ADMIN)
    verified = m4.source(admin, source_id)["last_verified_sync_at"]
    drive.fail_listing.add(drive.root_id)
    outcome = m4.sync(admin, source_id)
    assert outcome["status"] == "failed" and outcome["error_code"] == "drive_root_not_accessible"
    view = m4.source(admin, source_id)
    assert view["access"] == "last_sync_failed"
    assert view["last_verified_sync_at"] == verified  # never advanced by a failed run
    stream = answer(m4, FINANCE)
    assert stream.done["abstention"]["code"] == "no_sources"
    drive.fail_listing.clear()
    assert m4.sync(admin, source_id)["status"] == "succeeded"
    assert answer(m4, FINANCE).done["outcome"] == "published"


def test_subfolder_listing_failure_fails_the_sync(m4: M4) -> None:
    drive = m4.new_drive()
    sub = drive.folder("Sub")
    drive.file("in-sub.md", "# Sub\n\nSub text.\n", parent=sub, users=(m4.user_email(FINANCE),))
    admin = m4.token(ADMIN)
    source = m4.create_source(admin, drive.root_id)
    drive.fail_listing.add(sub)
    outcome = m4.sync(admin, source["source_id"])
    assert outcome["status"] == "failed" and outcome["error_code"] == "drive_unavailable"
    assert m4.source(admin, source["source_id"])["access"] == "never_verified"


def test_unreadable_item_permissions_deny_only_that_item(m4: M4) -> None:
    drive, source_id, file_id, doc = setup_source(m4)
    drive.unreadable_permissions.add(file_id)
    outcome = m4.sync(m4.token(ADMIN), source_id)
    assert outcome["status"] == "succeeded" and outcome["stats"]["permissions_unknown"] == 1
    state = owner_exec(
        m4.db, "SELECT permission_state FROM resources WHERE id = :d", {"d": doc}
    ).scalar_one()
    assert state == "unknown"
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
    drive.unreadable_permissions.clear()
    m4.sync(m4.token(ADMIN), source_id)
    assert answer(m4, FINANCE).done["outcome"] == "published"


def test_stale_verification_denies_without_any_sync(m4: M4) -> None:
    _, source_id, _, doc = setup_source(m4)
    owner_exec(
        m4.db,
        "UPDATE connector_sources SET last_verified_sync_at = now() - interval '31 minutes'"
        " WHERE id = :s",
        {"s": source_id},
    )
    assert m4.source(m4.token(ADMIN), source_id)["access"] == "stale"
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
    listed = m4.client.get("/api/v1/documents", headers=auth(m4.token(FINANCE))).json()
    assert doc not in {d["document_id"] for d in listed}
    m4.sync(m4.token(ADMIN), source_id)
    assert answer(m4, FINANCE).done["outcome"] == "published"


def test_paused_source_and_disabled_connector_deny_content(m4: M4) -> None:
    _, source_id, _, _ = setup_source(m4)
    admin = m4.token(ADMIN)
    paused = m4.client.patch(
        f"/api/v1/admin/sources/{source_id}", json={"status": "paused"}, headers=auth(admin)
    )
    assert paused.status_code == 200 and paused.json()["access"] == "paused"
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
    m4.client.patch(
        f"/api/v1/admin/sources/{source_id}", json={"status": "active"}, headers=auth(admin)
    )
    m4.sync(admin, source_id)
    assert answer(m4, FINANCE).done["outcome"] == "published"
    off = m4.client.put(
        "/api/v1/admin/tenant/settings",
        json={"enabled_connectors": ["upload"]},
        headers=auth(admin),
    )
    assert off.status_code == 200
    try:
        assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
        response = m4.client.post(f"/api/v1/admin/sources/{source_id}/sync", headers=auth(admin))
        assert (
            response.status_code == 403
            and response.json()["error"]["code"] == "capability_disabled"
        )
    finally:
        m4.client.put(
            "/api/v1/admin/tenant/settings",
            json={"enabled_connectors": ["upload", "google_drive"]},
            headers=auth(admin),
        )
    assert answer(m4, FINANCE).done["outcome"] == "published"


def test_source_attribute_change_is_denied_until_resynced(m4: M4) -> None:
    _, source_id, _, doc = setup_source(m4)
    admin = m4.token(ADMIN)
    m4.client.patch(
        f"/api/v1/admin/sources/{source_id}",
        json={"classification": "restricted"},
        headers=auth(admin),
    )
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
    m4.sync(admin, source_id)
    cls = owner_exec(
        m4.db, "SELECT classification FROM resources WHERE id = :d", {"d": doc}
    ).scalar_one()
    assert cls == "restricted"
    # finance-reader holds confidential clearance: the restricted source stays denied.
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"


def test_synced_documents_cannot_be_reshared_by_hand(m4: M4) -> None:
    _, _, _, doc = setup_source(m4)
    admin = m4.token(ADMIN)
    response = m4.client.put(
        f"/api/v1/documents/{doc}/permissions",
        json={"classification": "public", "grants": [{"group": "all-staff"}]},
        headers=auth(admin),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "managed_by_connector"
    assert m4.client.delete(f"/api/v1/documents/{doc}", headers=auth(admin)).status_code == 422


# -- sources: scope, operator allow-list, tenants -----------------------------------------


def test_source_creation_rules(m4: M4) -> None:
    admin = m4.token(ADMIN)
    bad_root = m4.client.post(
        "/api/v1/admin/sources",
        json={
            "name": "x",
            "external_root": "someoneElsesFolder",
            "department": "finance",
            "classification": "internal",
        },
        headers=auth(admin),
    )
    assert bad_root.status_code == 422 and bad_root.json()["error"]["code"] == "root_not_allowed"
    hr_source = m4.create_source(admin, m4.new_drive().root_id, department="hr")
    finance_admin = m4.token("niletech/finance-admin")
    listed = m4.client.get("/api/v1/admin/sources", headers=auth(finance_admin)).json()
    assert hr_source["source_id"] not in {s["source_id"] for s in listed}
    assert (
        m4.client.get(
            f"/api/v1/admin/sources/{hr_source['source_id']}", headers=auth(finance_admin)
        ).status_code
        == 404
    )
    forbidden = m4.client.post(
        "/api/v1/admin/sources",
        json={
            "name": "x",
            "external_root": m4.new_drive().root_id,
            "department": "hr",
            "classification": "internal",
        },
        headers=auth(finance_admin),
    )
    assert forbidden.status_code == 403
    reader = m4.token(FINANCE)
    assert m4.client.get("/api/v1/admin/sources", headers=auth(reader)).status_code == 403


def test_second_tenant_cannot_use_or_see_drive(m4: M4) -> None:
    _, source_id, _, _ = setup_source(m4)
    iso = m4.token("isolation-test/finance-admin")
    created = m4.client.post(
        "/api/v1/admin/sources",
        json={
            "name": "x",
            "external_root": m4.new_drive().root_id,
            "department": "finance",
            "classification": "internal",
        },
        headers=auth(iso),
    )
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "capability_disabled"
    assert m4.client.get("/api/v1/admin/sources", headers=auth(iso)).json() == []
    assert m4.client.get(f"/api/v1/admin/sources/{source_id}", headers=auth(iso)).status_code == 404


def test_connector_tables_are_tenant_isolated(m4: M4) -> None:
    setup_source(m4)
    iso_tenant = m4.db.foundation.tenants["isolation-test"].id
    nile_tenant = m4.db.foundation.tenants["niletech"].id
    engine = m4.client.app.state.engine  # type: ignore[attr-defined]
    for table in (
        "connector_sources",
        "connector_sync_runs",
        "drive_items",
        "external_group_links",
    ):
        with tenant_transaction(engine, iso_tenant) as conn:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
        with tenant_transaction(engine, nile_tenant) as conn:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() > 0  # noqa: S608
        rls = owner_exec(
            m4.db, "SELECT relrowsecurity FROM pg_class WHERE relname = :t", {"t": table}
        ).scalar_one()
        assert rls is True


# -- schedule -------------------------------------------------------------------------------


def test_schedule_is_fifteen_minutes_in_demo_and_nightly_otherwise(m4: M4) -> None:
    _, source_id, _, _ = setup_source(m4)
    nile = m4.db.foundation.tenants["niletech"].id

    def due() -> bool:
        rows = owner_exec(m4.db, "SELECT source_id FROM connector_sources_due(50)").all()
        return uuid.UUID(source_id) in {r.source_id for r in rows}

    def started(ago: str) -> None:
        owner_exec(
            m4.db,
            f"UPDATE connector_sources SET last_sync_started_at = now() - interval '{ago}'"  # noqa: S608
            " WHERE id = :s",
            {"s": source_id},
        )

    settings = owner_exec(
        m4.db,
        "SELECT sync_mode, sync_interval_seconds, max_staleness_seconds FROM tenant_settings"
        " WHERE tenant_id = :t",
        {"t": nile},
    ).one()
    assert tuple(settings) == ("demo", 900, 1800)
    started("14 minutes")
    assert not due()
    started("16 minutes")
    assert due()
    owner_exec(
        m4.db, "UPDATE tenant_settings SET sync_mode = 'nightly' WHERE tenant_id = :t", {"t": nile}
    )
    try:
        nightly = owner_exec(
            m4.db,
            "SELECT sync_interval_seconds, max_staleness_seconds FROM tenant_settings"
            " WHERE tenant_id = :t",
            {"t": nile},
        ).one()
        assert tuple(nightly) == (86400, 108000)
        started("16 minutes")
        # Nightly: due only if the last start precedes the most recent 02:00 UTC.
        boundary = owner_exec(
            m4.db,
            "SELECT now() - interval '16 minutes' < CASE WHEN now() >= date_trunc('day', now()"
            " AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '2 hours' THEN date_trunc('day',"
            " now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '2 hours' ELSE"
            " date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            " - interval '22 hours' END",
        ).scalar_one()
        assert due() is bool(boundary)
        started("25 hours")
        assert due()
    finally:
        owner_exec(
            m4.db, "UPDATE tenant_settings SET sync_mode = 'demo' WHERE tenant_id = :t", {"t": nile}
        )


# -- stale syncs are fenced out ---------------------------------------------------------------


def test_policy_change_during_a_sync_fences_the_running_sync(m4: M4) -> None:
    drive, source_id, file_id, doc = setup_source(m4)
    admin = m4.token(ADMIN)
    assert answer(m4, FINANCE).done["outcome"] == "published"

    def raise_classification() -> None:  # after the old run read the file's permissions
        response = m4.client.patch(
            f"/api/v1/admin/sources/{source_id}",
            json={"classification": "restricted"},
            headers=auth(admin),
        )
        assert response.status_code == 200, response.text

    drive.hooks[("list_permissions", file_id)] = raise_classification
    stale = m4.sync_direct(source_id)
    assert stale.status == "superseded"
    assert m4.run_row(stale.run_id).status == "superseded"
    row = m4.source_row(source_id)
    assert row.permissions_current is False and row.claim_run_id is None
    assert m4.source(admin, source_id)["access"] == "resync_required"
    seen_by = owner_exec(
        m4.db,
        "SELECT last_seen_run FROM drive_items WHERE source_id = :s AND external_id = :e",
        {"s": source_id, "e": file_id},
    ).scalar_one()
    assert seen_by != stale.run_id  # the stale run wrote nothing
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
    # The next sync applies the new policy: restricted is above the reader's clearance.
    fresh = m4.sync(admin, source_id)
    assert fresh["status"] == "succeeded"
    cls = owner_exec(
        m4.db, "SELECT classification FROM resources WHERE id = :d", {"d": doc}
    ).scalar_one()
    assert cls == "restricted"
    assert m4.source(admin, source_id)["access"] == "current"
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"


def test_newer_worker_after_lease_expiry_fences_the_older_one(m4: M4) -> None:
    drive, source_id, file_id, _ = setup_source(m4)
    assert answer(m4, FINANCE).done["outcome"] == "published"
    newer: list[Any] = []

    def take_over() -> None:
        # Worker A has read the old (shared) permissions. Sharing is then removed, A's
        # lease expires and worker B runs a complete sync.
        drive.unshare(file_id, m4.user_email(FINANCE))
        owner_exec(
            m4.db,
            "UPDATE connector_sources SET sync_claimed_until = now() - interval '1 second'"
            " WHERE id = :s",
            {"s": source_id},
        )
        newer.append(m4.sync_direct(source_id))

    drive.hooks[("list_permissions", file_id)] = take_over
    older = m4.sync_direct(source_id)
    (b,) = newer
    assert b.status == "succeeded" and b.stats["permissions_changed"] == 1
    assert older.status == "superseded"  # A could not write its stale "shared" grants
    row = m4.source_row(source_id)
    assert row.permissions_current is True and row.last_sync_status == "succeeded"
    assert row.last_verified_sync_at == m4.run_row(b.run_id).started_at
    assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"


def test_older_worker_cannot_tombstone_or_release_the_newer_claim(m4: M4) -> None:
    drive = m4.new_drive()
    reader = (m4.user_email(FINANCE),)
    first = drive.file("first.md", "# First\n\nFirst text.\n", users=reader)
    second = drive.file("second.md", "# Second\n\nSecond text.\n", users=reader)
    admin = m4.token(ADMIN)
    source_id = m4.create_source(admin, drive.root_id)["source_id"]
    m4.sync(admin, source_id)
    second_doc = m4.document_for(source_id, second)
    drive.delete(second)  # the older run's listing will not contain it
    service = m4.client.app.state.drive_sync  # type: ignore[attr-defined]
    tenant = m4.db.foundation.tenants["niletech"].id
    claims: list[Any] = []

    def take_over() -> None:
        owner_exec(
            m4.db,
            "UPDATE connector_sources SET sync_claimed_until = now() - interval '1 second'"
            " WHERE id = :s",
            {"s": source_id},
        )
        outcome, _, _ = service._claim(tenant, uuid.UUID(source_id), "scheduled", None, None)
        claims.append(outcome)

    drive.hooks[("list_permissions", first)] = take_over
    older = m4.sync_direct(source_id)
    (b,) = claims
    assert b.status == "running"
    assert older.status == "superseded"
    row = m4.source_row(source_id)
    assert row.claim_run_id == b.run_id and row.sync_claimed_until is not None
    deleted = owner_exec(
        m4.db, "SELECT deleted_at FROM documents WHERE id = :d", {"d": second_doc}
    ).scalar_one()
    assert deleted is None  # the stale run's tombstone never happened
    owner_exec(
        m4.db,
        "UPDATE connector_sources SET claim_run_id = NULL, sync_claimed_until = NULL WHERE id = :s",
        {"s": source_id},
    )


# -- operator allowlist on every sync -------------------------------------------------------


def test_root_removed_from_allowlist_fails_before_any_drive_request(m4: M4) -> None:
    drive, source_id, _, doc = setup_source(m4)
    assert answer(m4, FINANCE).done["outcome"] == "published"
    from tests.integration.m4_support import ALLOWED_ROOTS

    m4.set_allowed_roots([r for r in ALLOWED_ROOTS if r != drive.root_id])
    try:
        calls = len(drive.calls)
        outcome = m4.sync(m4.token(ADMIN), source_id)
        assert outcome["status"] == "failed" and outcome["error_code"] == "root_not_allowed"
        assert drive.calls[calls:] == []  # the folder was never requested
        assert m4.source(m4.token(ADMIN), source_id)["access"] == "last_sync_failed"
        assert answer(m4, FINANCE).done["abstention"]["code"] == "no_sources"
        listed = m4.client.get("/api/v1/documents", headers=auth(m4.token(FINANCE))).json()
        assert doc not in {d["document_id"] for d in listed}
    finally:
        m4.set_allowed_roots(ALLOWED_ROOTS)


def test_sync_request_returns_its_own_run(m4: M4) -> None:
    _, source_id, _, _ = setup_source(m4)
    admin = m4.token(ADMIN)
    response = m4.sync(admin, source_id)
    run = m4.client.get(
        f"/api/v1/admin/sources/{source_id}/runs/{response['run_id']}", headers=auth(admin)
    ).json()
    assert run["status"] == "succeeded" and run["terminal"] is True
    # A run for which the worker cannot claim the source is recorded, not silent.
    owner_exec(
        m4.db,
        "UPDATE connector_sources SET sync_claimed_until = now() + interval '5 minutes'"
        " WHERE id = :s",
        {"s": source_id},
    )
    try:
        busy = m4.sync(admin, source_id)
        run = m4.client.get(
            f"/api/v1/admin/sources/{source_id}/runs/{busy['run_id']}", headers=auth(admin)
        ).json()
        assert (run["status"], run["error_code"]) == ("skipped", "sync_in_progress")
    finally:
        owner_exec(
            m4.db,
            "UPDATE connector_sources SET sync_claimed_until = NULL WHERE id = :s",
            {"s": source_id},
        )
