"""M7 evaluation foundation: frozen splits, freeze enforcement, provenance, pilot, labels.

Every failure the M7 smoke check claims to catch is reproduced here on a mutated copy, so
a check that passes everything would fail these tests. The PR #10 review findings each
have their own section.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.evaluation import freeze
from app.evaluation import labels as lab
from app.evaluation import manifest as mf
from app.evaluation.checks import check_records, legacy_assignments, run_checks
from app.evaluation.pilot import build_records, render

M7 = mf.REPO / freeze.M7_REL
V1_TEXT = (M7 / "split-manifest-v1.toml").read_text("utf-8")
V2_TEXT = (M7 / "split-manifest-v2.toml").read_text("utf-8")
V3_TEXT = (M7 / "split-manifest-v3.toml").read_text("utf-8")  # active
CODE_DEV = 'id = "code-config-constants"\nsplit = "dev"'
BOARD_DEV = 'id = "board-governance"\nsplit = "dev"'


def _parse(text: str) -> mf.Manifest:
    manifest, problems = mf.parse_manifest(text)
    assert problems == []
    return manifest


# ------------------------------------------------------------------ repository state
def test_committed_state_passes_the_m7_check() -> None:
    problems, summary = run_checks()
    assert problems == []
    assert "PILOT datasets" in summary and "Cohen's kappa" in summary


def test_pilot_build_is_byte_identical_across_runs() -> None:
    assert render() == render()


def test_pilot_counts_are_all_dev() -> None:
    data = build_records()
    assert {k: len(v) for k, v in data.items()} == {
        "qa-cases": 12,
        "retrieval-pairs": 57,
        "checker-train-pairs": 78,
        "gold-checker-pairs": 37,
    }
    every = [r for recs in data.values() for r in recs]
    assert {r["split"] for r in every} == {"dev"}
    assert all(r["pilot"] is True and r["release"] is False for r in every)
    index = json.loads(render()["datasets.json"])
    assert {n: d["in_intended_split"] for n, d in index["datasets"].items()} == dict.fromkeys(
        index["datasets"], 0
    )


def test_gold_pairs_never_carry_a_label_field_only_a_proposal() -> None:
    for rec in build_records()["gold-checker-pairs"]:
        assert "label" not in rec and rec["proposed_label"] in lab.LABELS


# ------------------------------------------- finding 3: split inventory and exposure
def test_v3_assigns_every_source_once_and_passes_the_exposure_audit() -> None:
    manifest = _parse(V3_TEXT)
    splits = list(manifest.family_split.values())
    assert (splits.count("dev"), splits.count("train"), splits.count("heldout")) == (9, 3, 2)
    assert manifest.reserved == {}
    assert mf.audit_exposure(manifest) == []


def test_v3_keeps_the_nine_v2_dev_families_unchanged() -> None:
    v2, _ = mf.parse_manifest(V2_TEXT)  # superseded: fresh files are unassigned there
    v3 = _parse(V3_TEXT)
    assert v2.family_split == {f: s for f, s in v3.family_split.items() if f in v2.family_split}
    assert set(v2.family_split.values()) == {"dev"}
    assert {r: f for r, f in v3.member_family.items() if not r.startswith("fresh:")} == (
        v2.member_family
    )


def test_v1_is_kept_and_its_misassignments_are_caught_by_the_audit() -> None:
    problems = mf.audit_exposure(mf.parse_manifest(V1_TEXT)[0])
    flagged = {p.split()[1] for p in problems if "development artifacts mention it" in p}
    assert flagged == {"code-config-constants", "contract-template", "board-governance"}


def test_moving_an_exposed_family_out_of_dev_fails_the_audit() -> None:
    text = V3_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"')
    problems = mf.audit_exposure(_parse(text))
    assert any("board-governance is heldout but development artifacts" in p for p in problems)


def test_listed_exposure_must_exist_and_mention_a_member() -> None:
    text = V3_TEXT.replace(
        'exposure = ["backend/tests/integration/test_m5_legal.py"]',
        'exposure = ["backend/tests/unit/test_policy_evaluator.py", "docs/nope.md"]',
    )
    problems = mf.audit_exposure(_parse(text))
    assert any("test_policy_evaluator.py mentions no member" in p for p in problems)
    assert any("docs/nope.md does not exist" in p for p in problems)


def test_unassigned_source_and_double_assignment_fail() -> None:
    _, problems = mf.parse_manifest(
        V3_TEXT.replace('members = ["doc:customers-csv"]', "members = []")
    )
    assert any("doc:customers-csv is not assigned" in p for p in problems)
    _, problems = mf.parse_manifest(
        V3_TEXT.replace(
            'members = ["doc:customers-csv"]',
            'members = ["doc:customers-csv", "doc:hr-leave-policy"]',
        )
    )
    assert any("doc:hr-leave-policy is in two families" in p for p in problems)


def test_reserved_families_cannot_hold_members() -> None:
    text = V3_TEXT + '\n[[reserved_families]]\nid = "r"\nsplit = "heldout"\nmembers = ["doc:x"]\n'

    _, problems = mf.parse_manifest(text)
    assert any("must not have members" in p for p in problems)


def test_moving_a_dev_exposed_family_is_caught_via_legacy_fixtures() -> None:
    text = V3_TEXT.replace(
        'id = "hr-leave-policy"\nsplit = "dev"', 'id = "hr-leave-policy"\nsplit = "heldout"'
    )
    _, problems = legacy_assignments(_parse(text))
    assert any("hr-leave-policy is heldout" in p and "cross splits" in p for p in problems)


# --------------------------------------------------- finding 4: enforceable freeze
def _tampered_overlay(text: str) -> dict[str, str]:
    """Change the manifest, then regenerate the lock AND the pilot to match it."""

    manifest = _parse(text)
    overlay = {
        "split-manifest-v3.toml": text,
        "split-manifest-v3.lock.json": mf.lock_text(
            mf.build_lock(manifest, "split-manifest-v3.toml")
        ),
    }
    overlay.update({f"pilot/{rel}": body for rel, body in render(manifest).items()})
    return overlay


def test_changed_manifest_with_regenerated_lock_and_pilot_still_fails() -> None:
    text = V3_TEXT.replace(
        'reason = "M3 retrieval dev cases; M1 parser-benchmark gold document."',
        'reason = "Edited after the freeze."',
    )
    problems, _ = run_checks(overlay=_tampered_overlay(text))
    assert problems, "a regenerated lock and pilot must not hide a manifest edit"
    assert any("does not match the reviewed snapshot" in p for p in problems)
    # The lock and pilot really were regenerated consistently: only the freeze fails.
    assert all(p.startswith("freeze:") for p in problems), problems


def test_editing_a_superseded_manifest_or_lock_fails() -> None:
    problems, _ = run_checks(overlay={"split-manifest-v1.toml": V1_TEXT + "\n# edit\n"})
    assert any("split-manifest-v1.toml does not match the reviewed snapshot" in p for p in problems)
    lock = json.loads((M7 / "split-manifest-v1.lock.json").read_text("utf-8"))
    lock["member_sha256"]["doc:hr-leave-policy"] = "0" * 64
    problems, _ = run_checks(overlay={"split-manifest-v1.lock.json": mf.lock_text(lock)})
    assert any("split-manifest-v1.lock.json does not match" in p for p in problems)


def test_changed_member_source_fails_the_lock() -> None:
    manifest = _parse(V3_TEXT)
    lock = mf.load_lock(M7 / "split-manifest-v3.lock.json")
    lock["member_sha256"]["doc:hr-leave-policy"] = "0" * 64
    assert any("doc:hr-leave-policy changed" in p for p in mf.verify_lock(manifest, lock))


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return done.stdout.decode()


@pytest.fixture
def frozen_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo whose `main` holds the current frozen M7 files."""

    repo = tmp_path / "repo"
    m7 = repo / freeze.M7_REL
    m7.mkdir(parents=True)
    for name in ("split-manifest-v1.toml", "split-manifest-v1.lock.json",
                 "split-manifest-v2.toml", "split-manifest-v2.lock.json",
                 "split-manifest-v3.toml", "split-manifest-v3.lock.json",
                 "frozen-snapshots.json"):  # fmt: skip
        shutil.copy(M7 / name, m7 / name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "freeze")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo, m7


def test_rewriting_manifest_lock_and_snapshot_together_fails_against_base(
    frozen_repo: tuple[Path, Path],
) -> None:
    repo, m7 = frozen_repo
    overlay = _tampered_overlay(
        V3_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"')
    )
    (m7 / "split-manifest-v3.toml").write_text(overlay["split-manifest-v3.toml"])
    (m7 / "split-manifest-v3.lock.json").write_text(overlay["split-manifest-v3.lock.json"])
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    entry = snaps["manifests"]["m7-split-v3"]
    entry["manifest_sha256"] = freeze._sha(overlay["split-manifest-v3.toml"])
    entry["lock_sha256"] = freeze._sha(overlay["split-manifest-v3.lock.json"])
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    _git(repo, "commit", "-q", "-am", "rewrite the freeze")
    problems = freeze.check_against_base("main", repo, freeze.M7_REL)
    assert any("split-manifest-v3.toml is frozen on main" in p for p in problems)
    assert any("split-manifest-v3.lock.json is frozen on main" in p for p in problems)
    assert any("m7-split-v3.manifest_sha256 on main was changed" in p for p in problems)


def test_base_allows_only_active_to_superseded(frozen_repo: tuple[Path, Path]) -> None:
    repo, m7 = frozen_repo
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    snaps["manifests"]["m7-split-v3"].update(status="superseded", superseded_by="v4", reason="r")
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    assert freeze.check_against_base("main", repo, freeze.M7_REL) == []
    snaps["manifests"]["m7-split-v1"]["status"] = "active"
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    problems = freeze.check_against_base("main", repo, freeze.M7_REL)
    assert any("superseded -> active is not allowed" in p for p in problems)


def test_freeze_must_be_committed_before_the_pilot(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    m7 = repo / freeze.M7_REL
    (m7 / "pilot").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (m7 / "lock.json").write_text("{}")
    (m7 / "pilot" / "datasets.json").write_text("{}")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "lock and pilot together")
    together = freeze.check_freeze_order("lock.json", "pilot/datasets.json", repo)
    assert any("committed together" in p for p in together)
    (m7 / "pilot" / "datasets.json").write_text('{"regenerated": true}')
    _git(repo, "commit", "-q", "-am", "regenerate pilot after the freeze")
    assert freeze.check_freeze_order("lock.json", "pilot/datasets.json", repo) == []


# ------------------------------------------------- finding 2: evidence and provenance
@pytest.fixture
def train_manifest() -> mf.Manifest:
    """v2 with the code family moved to train, so train/dev crossings can be built."""

    return _parse(V3_TEXT.replace(CODE_DEV, 'id = "code-config-constants"\nsplit = "train"'))


@pytest.fixture
def data(train_manifest: mf.Manifest) -> dict[str, list[dict[str, Any]]]:
    return copy.deepcopy(build_records(train_manifest))


def _problems(manifest: mf.Manifest, data: dict[str, list[dict[str, Any]]]) -> list[str]:
    return check_records(manifest, data)


def _find(recs: list[dict[str, Any]], pred: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    return next(r for r in recs if pred(r))


def test_untouched_records_pass_with_a_train_family(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    assert {r["split"] for r in data["checker-train-pairs"]} == {"dev", "train"}
    assert _problems(train_manifest, data) == []


REFUND_SPAN = "# The refund processing fee is 2.5 percent of the order total."


def test_dev_claim_with_undeclared_train_evidence_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    rec = _find(data["gold-checker-pairs"], lambda r: r["id"] == "gold-hr-egypt-days:supported")
    rec.update(evidence_source="code:billing/refunds.py", evidence=REFUND_SPAN)
    problems = _problems(train_manifest, data)
    assert any(
        "gold-hr-egypt-days:supported: uses evidence from code:billing/refunds.py, which it "
        "does not declare" in p
        for p in problems
    )
    assert any("several families" in p and "'dev', 'train'" in p for p in problems)


def test_dev_claim_with_declared_train_evidence_still_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    rec = _find(data["gold-checker-pairs"], lambda r: r["id"] == "gold-hr-egypt-days:supported")
    rec.update(evidence_source="code:billing/refunds.py", evidence=REFUND_SPAN)
    rec["sources"] = sorted({*rec["sources"], "code:billing/refunds.py"})
    problems = _problems(train_manifest, data)
    assert any("gold-hr-egypt-days:supported: cites sources from several families" in p
               for p in problems)  # fmt: skip


def test_qa_claim_citing_an_undeclared_source_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    qa = _find(data["qa-cases"], lambda r: r["id"] == "qa-pilot-007")
    qa["claims"][0]["source"] = "doc:hr-leave-policy"
    problems = _problems(train_manifest, data)
    assert any("qa-pilot-007: uses evidence from doc:hr-leave-policy" in p for p in problems)


def test_source_not_in_manifest_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    data["retrieval-pairs"][0]["sources"].append("doc:not-in-manifest")
    assert any("doc:not-in-manifest is not in the manifest" in p
               for p in _problems(train_manifest, data))  # fmt: skip


def test_train_record_pointing_to_a_real_dev_seed_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    rec = _find(data["checker-train-pairs"], lambda r: r["family"] == "code-config-constants")
    assert rec["split"] == "train"
    rec["derived_from"] = "seed:gold-hr-egypt-days"  # a real seed, in a dev family
    problems = _problems(train_manifest, data)
    assert any(
        f"{rec['id']}: derived from seed:gold-hr-egypt-days in family hr-leave-policy (dev), "
        "but the record is code-config-constants (train)" in p
        for p in problems
    )


def test_train_record_pointing_to_a_real_dev_legacy_item_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    rec = _find(data["retrieval-pairs"], lambda r: r["id"] == "ret-pilot-code-refund-fee")
    assert rec["split"] == "train"
    rec["derived_from"] = "legacy:fixtures/evaluation/documents-dev-m3.toml#fin-net-q1"
    problems = _problems(train_manifest, data)
    assert any("ret-pilot-code-refund-fee: derived from legacy:" in p and "(dev)" in p
               for p in problems)  # fmt: skip


@pytest.mark.parametrize(
    ("parent", "message"),
    [
        ("seed:no-such-seed", "unknown parent seed:no-such-seed (no such seed)"),
        (
            "legacy:fixtures/evaluation/documents-dev-m3.toml#no-such-case",
            "(no such fixture item)",
        ),
        ("legacy:fixtures/evaluation/other.toml#x", "(fixture not in the manifest)"),
        ("", "derived_from must be seed:<id> or legacy:<path>#<id>"),
    ],
)
def test_unknown_parents_fail(train_manifest, data, parent: str, message: str) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["derived_from"] = parent
    assert any(message in p for p in _problems(train_manifest, data))


def test_record_in_the_wrong_split_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["split"] = "train"
    assert any("so the record crosses splits" in p for p in _problems(train_manifest, data))


def test_sealed_heldout_family_fails() -> None:
    manifest = _parse(V3_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"'))
    data = copy.deepcopy(build_records(manifest))
    rec = data["retrieval-pairs"][0]
    rec.update(
        sources=["doc:board-minutes"],
        source="doc:board-minutes",
        family="board-governance",
        split="heldout",
        quote="Riyadh office",
    )
    assert any(
        "sealed held-out family board-governance" in p for p in check_records(manifest, data)
    )


def test_same_text_in_two_splits_fails(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    dev_claim = data["gold-checker-pairs"][0]["claim"]
    rec = _find(data["checker-train-pairs"], lambda r: r["split"] == "train")
    rec["claim"] = dev_claim.replace("21", "22")
    assert any("text appears in splits ['dev', 'train']" in p
               for p in _problems(train_manifest, data))  # fmt: skip


def test_evidence_must_be_an_exact_span(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["evidence"] += " (paraphrased)"
    assert any("not an exact span" in p for p in _problems(train_manifest, data))


def test_sql_claim_must_state_its_fixture_value(train_manifest, data) -> None:  # type: ignore[no-untyped-def]
    qa = _find(data["qa-cases"], lambda r: r["id"] == "qa-pilot-001")
    qa["claims"][0]["text"] = "NileTech's net fulfilled sales in Q1 2026 were USD 80.9 million."
    assert any("does not state fixture value 80934385.00" in p
               for p in _problems(train_manifest, data))  # fmt: skip


@pytest.mark.parametrize(
    "claim",
    [
        "Egypt gets 21 days; the UAE gets 30 days.",
        "Egypt gets 21 days. The UAE gets 30 days.",
        " ".join(["word"] * 31) + ".",
    ],
)
def test_non_atomic_claims_fail(train_manifest, data, claim: str) -> None:  # type: ignore[no-untyped-def]
    rec = data["gold-checker-pairs"][0]
    rec["claim"] = claim
    assert any("claim" in p and rec["id"] in p for p in _problems(train_manifest, data))


# ------------------------------------------------------- finding 1: blind annotation
def test_cohen_kappa_known_value_and_edges() -> None:
    a = ["s"] * 20 + ["u"] * 15 + ["s"] * 5 + ["u"] * 10
    b = ["s"] * 20 + ["u"] * 15 + ["u"] * 5 + ["s"] * 10
    assert lab.cohen_kappa(a, b) == pytest.approx(0.4)  # po 0.7, pe 0.5
    assert lab.cohen_kappa([], []) is None
    assert lab.cohen_kappa(["s", "s"], ["s", "s"]) is None
    assert lab.cohen_kappa(["s", "u"], ["s", "u"]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        lab.cohen_kappa(["s"], [])


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open(encoding="utf-8")))


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fill(sheet: Path, key: Path, decide: Callable[[str], str]) -> None:
    mapping = json.loads(key.read_text())["items"]
    rows = _rows(sheet)
    for row in rows:
        row["label"] = decide(mapping[row["item_id"]])
    _write_rows(sheet, rows)


def test_sheets_are_blind(tmp_path: Path) -> None:
    sheet_a, key_a = lab.export_sheet("alice", tmp_path)
    sheet_b, _ = lab.export_sheet("bob", tmp_path)
    rows_a, rows_b = _rows(sheet_a), _rows(sheet_b)
    assert tuple(rows_a[0]) == lab.SHEET_COLUMNS
    pairs = lab.gold_pairs()
    sheet_text = sheet_a.read_text("utf-8")
    for p in pairs:
        assert p["id"] not in sheet_text
    assert "proposed" not in sheet_text
    # Pair ids encode the perturbation kind (":p1-number", ":wrong-evidence", ":supported").
    assert not re.search(r":(supported|wrong-evidence|p\d+-[a-z_]+)\b", sheet_text)
    assert not any(p["kind"] in {r["item_id"] for r in rows_a} for p in pairs)
    assert all(re.fullmatch(r"item-[0-9a-f]{12}", r["item_id"]) for r in rows_a)
    assert all(r["label"] == "" for r in rows_a)
    assert {r["item_id"] for r in rows_a}.isdisjoint({r["item_id"] for r in rows_b})
    claims_a = [r["claim"] for r in rows_a]
    assert claims_a != [r["claim"] for r in rows_b]  # independent order
    assert sorted(claims_a) == sorted(p["claim"] for p in pairs)
    assert key_a.name.endswith(".key.json")


def test_export_import_round_trip_maps_items_back_to_pairs(tmp_path: Path) -> None:
    sheets = tmp_path / "sheets"
    labels_dir = tmp_path / "labels"
    proposed = {p["id"]: p["proposed_label"] for p in lab.gold_pairs()}
    flipped = sorted(proposed)[0]
    sa, ka = lab.export_sheet("alice", sheets)
    sb, kb = lab.export_sheet("bob", sheets)
    _fill(sa, ka, lambda pid: proposed[pid])
    other = {"supported": "unsupported", "unsupported": "supported"}
    _fill(sb, kb, lambda pid: other[proposed[pid]] if pid == flipped else proposed[pid])
    written = lab.import_sheets([(sa, ka), (sb, kb)], labels_dir)
    assert set(written) == {"alice", "bob"}
    alice = {r["pair_id"]: r["label"] for r in _rows(written["alice"])}
    assert alice == proposed  # every opaque id mapped back to the right pair
    status = lab.status(labels_dir)
    assert status["double_labelled"] == len(proposed)
    assert [d["pair_id"] for d in status["disagreements"]] == [flipped]
    assert status["state_counts"] == {"agreed": len(proposed) - 1, "disagreement-pending": 1}
    assert status["problems"] == []
    assert status["cohen_kappa"] is not None and 0.8 < status["cohen_kappa"] < 1.0

    _write_rows(labels_dir / lab.ADJUDICATIONS, [
        {"pair_id": flipped, "final_label": proposed[flipped], "adjudicator": "carol",
         "reason": "evidence states it"},
    ])  # fmt: skip
    after = lab.status(labels_dir)
    assert after["adjudicated"] == 1 and after["gold_labels"] == len(proposed)
    assert after["problems"] == []


@pytest.fixture
def two_sheets(tmp_path: Path) -> list[tuple[Path, Path]]:
    out = []
    for name in ("alice", "bob"):
        sheet, key = lab.export_sheet(name, tmp_path / "sheets")
        _fill(sheet, key, lambda _pid: "supported")
        out.append((sheet, key))
    return out


def test_sheet_from_another_dataset_revision_is_rejected(two_sheets, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    sheet, _ = two_sheets[0]
    rows = _rows(sheet)
    rows[0]["dataset_revision"] = "0" * 64
    _write_rows(sheet, rows)
    with pytest.raises(lab.LabelError, match="from dataset revision 000000000000"):
        lab.import_sheets(two_sheets, tmp_path / "labels")
    assert not (tmp_path / "labels").exists()  # nothing written


def test_sheets_exported_before_the_gold_pairs_changed_are_rejected(
    two_sheets: list[tuple[Path, Path]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "dataset_revision", lambda: "f" * 64)
    with pytest.raises(lab.LabelError, match="re-export the sheets"):
        lab.import_sheets(two_sheets, tmp_path / "labels")


def test_import_requires_both_annotators_together(two_sheets, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(lab.LabelError, match="exactly two submissions together"):
        lab.import_sheets(two_sheets[:1], tmp_path / "labels")
    with pytest.raises(lab.LabelError, match="different annotators"):
        lab.import_sheets([two_sheets[0], two_sheets[0]], tmp_path / "labels")
    lab.import_sheets(two_sheets, tmp_path / "labels")
    with pytest.raises(lab.LabelError, match="already imported"):
        lab.import_sheets(two_sheets, tmp_path / "labels")


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda rows: rows[0].update(item_id="item-000000000000"), "unknown item"),
        (lambda rows: rows.pop(), "deleted from the sheet"),
        (lambda rows: rows.append(dict(rows[0])), "appears twice"),
        (lambda rows: rows[0].update(label="maybe"), "invalid label 'maybe'"),
        (lambda rows: rows[0].update(annotator="mallory"), "belongs to 'mallory'"),
    ],
)
def test_tampered_sheets_are_rejected(two_sheets, tmp_path: Path, edit, message: str) -> None:  # type: ignore[no-untyped-def]
    sheet, _ = two_sheets[1]
    rows = _rows(sheet)
    edit(rows)
    _write_rows(sheet, rows)
    with pytest.raises(lab.LabelError, match=re.escape(message)):
        lab.import_sheets(two_sheets, tmp_path / "labels")


def test_swapped_keys_are_rejected(two_sheets, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    (sa, ka), (sb, kb) = two_sheets
    with pytest.raises(lab.LabelError):
        lab.import_sheets([(sa, kb), (sb, ka)], tmp_path / "labels")


def test_label_files_are_validated(tmp_path: Path) -> None:
    ids = [p["id"] for p in lab.gold_pairs()][:2]
    rev = lab.dataset_revision()
    _write_rows(tmp_path / "gold-alice.csv", [
        {"pair_id": ids[0], "label": "maybe", "note": "", "dataset_revision": rev},
        {"pair_id": "no-such-pair", "label": "supported", "note": "", "dataset_revision": rev},
        {"pair_id": ids[1], "label": "supported", "note": "", "dataset_revision": "old"},
    ])  # fmt: skip
    _write_rows(tmp_path / lab.ADJUDICATIONS, [
        {"pair_id": ids[1], "final_label": "supported", "adjudicator": "", "reason": ""},
    ])  # fmt: skip
    problems = lab.status(tmp_path)["problems"]
    assert any("invalid label 'maybe'" in p for p in problems)
    assert any("unknown pair 'no-such-pair'" in p for p in problems)
    assert any("labelled on another dataset revision" in p for p in problems)
    assert any("only one annotator's labels are imported" in p for p in problems)
    assert any("no adjudicator" in p for p in problems)
    assert any("not a recorded disagreement" in p for p in problems)


# ------------------------------------- re-review: content, completeness, atomic publish
@pytest.mark.parametrize("field", ["claim", "evidence_title", "evidence"])
def test_edited_sheet_content_is_rejected(
    two_sheets: list[tuple[Path, Path]], tmp_path: Path, field: str
) -> None:
    sheet, _ = two_sheets[1]
    rows = _rows(sheet)
    rows[3][field] = rows[3][field].replace("e", "E", 1) + " (edited)"
    _write_rows(sheet, rows)
    with pytest.raises(lab.LabelError, match=f"has edited {field}; only label and note"):
        lab.import_sheets(two_sheets, tmp_path / "labels")
    assert not (tmp_path / "labels").exists()  # the other, valid sheet is not published


def test_content_swapped_between_rows_is_rejected(
    two_sheets: list[tuple[Path, Path]], tmp_path: Path
) -> None:
    sheet, _ = two_sheets[0]
    rows = _rows(sheet)
    rows[0]["claim"], rows[1]["claim"] = rows[1]["claim"], rows[0]["claim"]
    _write_rows(sheet, rows)
    with pytest.raises(lab.LabelError, match="has edited claim"):
        lab.import_sheets(two_sheets, tmp_path / "labels")


def test_blank_sheet_is_rejected_and_nothing_is_published(tmp_path: Path) -> None:
    sa, ka = lab.export_sheet("alice", tmp_path / "sheets")
    sb, kb = lab.export_sheet("bob", tmp_path / "sheets")
    _fill(sa, ka, lambda _pid: "supported")  # alice complete, bob untouched
    n = len(lab.gold_pairs())
    with pytest.raises(lab.LabelError, match=f"{n} of {n} item\\(s\\) have no label"):
        lab.import_sheets([(sa, ka), (sb, kb)], tmp_path / "labels")
    assert not (tmp_path / "labels").exists()


def test_incomplete_sheet_is_rejected_and_nothing_is_published(
    two_sheets: list[tuple[Path, Path]], tmp_path: Path
) -> None:
    sheet, _ = two_sheets[0]
    rows = _rows(sheet)
    rows[-1]["label"] = "  "
    _write_rows(sheet, rows)
    n = len(rows)
    with pytest.raises(lab.LabelError, match=f"1 of {n} item\\(s\\) have no label"):
        lab.import_sheets(two_sheets, tmp_path / "labels")
    assert not (tmp_path / "labels").exists()


def _published(label_dir: Path) -> list[str]:
    return sorted(p.name for p in label_dir.iterdir()) if label_dir.exists() else []


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_write_failure_leaves_nothing_and_a_retry_succeeds(
    two_sheets: list[tuple[Path, Path]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_on_call: int,
) -> None:
    label_dir = tmp_path / "labels"
    real_publish = lab._publish
    calls = {"n": 0}

    def flaky(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise OSError("simulated disk failure")
        real_publish(src, dst)

    monkeypatch.setattr(lab, "_publish", flaky)
    with pytest.raises(lab.LabelError, match="rolled back; nothing was published"):
        lab.import_sheets(two_sheets, label_dir)
    assert calls["n"] == fail_on_call
    assert _published(label_dir) == []  # no label file, no staging directory
    assert lab.status(label_dir)["human_reviewed_pair_ids"] == []

    monkeypatch.setattr(lab, "_publish", real_publish)
    written = lab.import_sheets(two_sheets, label_dir)
    assert _published(label_dir) == ["gold-alice.csv", "gold-bob.csv"]
    assert set(written) == {"alice", "bob"}
    status = lab.status(label_dir)
    assert status["problems"] == []
    assert status["double_labelled"] == len(lab.gold_pairs())


# ------------------------------------------------------------- v3 fresh corpus
from app.evaluation import corpus_v3, overlap  # noqa: E402
from app.evaluation.checks import corpus_assignment_problems  # noqa: E402


def test_v3_corpus_is_deterministic_and_matches_the_committed_files() -> None:
    assert corpus_v3.generate() == corpus_v3.generate()
    assert corpus_v3.check_corpus() == []
    index = overlap.load_corpus_index()
    assert index["seed"] == corpus_v3.SEED == 20261126
    assert index["generator_sha256"] == corpus_v3.generator_sha256()
    assert index["original_generator"]["commit"].startswith("bf4a247")
    counts: dict[str, int] = {}
    for doc in index["documents"]:
        counts[doc["split"]] = counts.get(doc["split"], 0) + 1
    assert counts == {"train": 60, "heldout": 24}


def test_v3_corpus_rebuild_catches_a_changed_seed_or_edited_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus_v3.write_corpus(tmp_path)
    assert corpus_v3.check_corpus(tmp_path) == []
    (tmp_path / "files" / "v3-wo-001.md").write_text("edited\n")
    assert any("v3-wo-001.md differs" in p for p in corpus_v3.check_corpus(tmp_path))
    corpus_v3.write_corpus(tmp_path)
    monkeypatch.setattr(corpus_v3, "SEED", 1)
    assert len(corpus_v3.check_corpus(tmp_path)) > 50


def _fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B')} {d.year}"


def test_every_fact_is_stated_in_its_document() -> None:
    index = overlap.load_corpus_index()
    base = M7 / "corpus-v3"
    for doc in index["documents"]:
        text = (base / doc["file"]).read_text("utf-8")
        facts = json.loads((base / doc["facts"]).read_text("utf-8"))
        for key, value in facts.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                assert f"{value:,}" in text or str(value) in text, (doc["key"], key)
            elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d.*", value):
                assert _fmt_date(value) in text, (doc["key"], key)
            elif isinstance(value, str):
                assert value in text, (doc["key"], key)
            else:
                for q in value:
                    assert q["supplier"] in text and f"{q['unit_egp']:,}" in text


def test_v3_fresh_families_do_not_overlap_dev_or_each_other() -> None:
    report = overlap.overlap_report(_parse(V3_TEXT))
    assert overlap.overlap_problems(report) == []
    assert report["dev_texts_compared"] >= 20
    assert len(report["family_pairs"]) == 10


def _heldout_doc_text() -> str:
    return (M7 / "corpus-v3" / "files" / "v3-ps-001.md").read_text("utf-8")


def test_overlap_check_catches_dev_text_copied_into_a_fresh_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = overlap._dev_texts
    copied = _heldout_doc_text().split("## Progress")[1]
    monkeypatch.setattr(overlap, "_dev_texts", lambda m: {**real(m), "doc:fake": copied})
    problems = overlap.overlap_problems(overlap.overlap_report(_parse(V3_TEXT)))
    assert any("fresh-heldout-regenerated-a shares template text with dev" in p for p in problems)


def test_overlap_check_catches_a_reused_fact_name(monkeypatch: pytest.MonkeyPatch) -> None:
    real = overlap._dev_texts
    monkeypatch.setattr(
        overlap,
        "_dev_texts",
        lambda m: {**real(m), "doc:fake": "We hired Karnak Facility Services."},
    )
    problems = overlap.overlap_problems(overlap.overlap_report(_parse(V3_TEXT)))
    assert any("fresh-train-regenerated reuses names" in p and "Karnak" in p for p in problems)


def test_overlap_check_catches_a_template_shared_across_splits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = overlap._fresh_docs
    train_text = (M7 / "corpus-v3" / "files" / "v3-pir-001.md").read_text("utf-8")

    def leaky(m: mf.Manifest) -> dict[str, tuple[str, str, str]]:
        docs = real(m)
        fam, split, text = docs["fresh:v3-pr-001"]
        docs["fresh:v3-pr-001"] = (fam, split, text + "\n" + train_text)
        return docs

    monkeypatch.setattr(overlap, "_fresh_docs", leaky)
    problems = overlap.overlap_problems(overlap.overlap_report(_parse(V3_TEXT)))
    assert any(
        "fresh-heldout-regenerated-b and fresh-train-incident-review share" in p
        or "fresh-train-incident-review and fresh-heldout-regenerated-b share" in p
        for p in problems
    )


def test_a_development_artifact_mentioning_heldout_material_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = mf._artifact_texts()
    monkeypatch.setattr(
        mf, "_artifact_texts", lambda: (*real, ("ml/tune_threshold.py", "load('v3-ps-004.md')"))
    )
    problems = mf.audit_exposure(_parse(V3_TEXT))
    assert any(
        "fresh-heldout-regenerated-a is heldout but development artifacts mention it "
        "(ml/tune_threshold.py)" in p
        for p in problems
    )


def test_generated_split_must_match_the_frozen_manifest() -> None:
    text = V3_TEXT.replace(
        'id = "fresh-heldout-regenerated-a"\nsplit = "heldout"',
        'id = "fresh-heldout-regenerated-a"\nsplit = "train"',
    )
    problems = corpus_assignment_problems(_parse(text))
    assert any(
        "fresh:v3-ps-001: generated as fresh-heldout-regenerated-a (heldout) but the manifest "
        "has fresh-heldout-regenerated-a (train)" in p
        for p in problems
    )
    assert corpus_assignment_problems(_parse(V3_TEXT)) == []


def test_pilot_records_cannot_use_a_fresh_heldout_family() -> None:
    manifest = _parse(V3_TEXT)
    data = copy.deepcopy(build_records(manifest))
    rec = data["retrieval-pairs"][0]
    rec.update(
        sources=["fresh:v3-ps-001"],
        source="fresh:v3-ps-001",
        family="fresh-heldout-regenerated-a",
        split="heldout",
        quote=_heldout_doc_text().split("\n")[0][2:],
    )
    problems = check_records(manifest, data)
    assert any("sealed held-out family fresh-heldout-regenerated-a" in p for p in problems)


# ------------------------------------------------- split-enforcing loader (re-review)
from app.evaluation import loader  # noqa: E402

_PROBE = r"""
import json, sys
events = []
def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)) or event == "open" and hasattr(args[0], "__fspath__"):
        events.append(("open", str(args[0])))
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
        events.append(("list", str(args[0])))
sys.addaudithook(hook)
from pathlib import Path
from app.evaluation import loader
kw = json.loads(sys.argv[2])
kw = {k: Path(v) for k, v in kw.items()}
result = loader.load(sys.argv[1], **kw)
print(json.dumps({
    "events": events,
    "loaded": [[d.ref, d.split] for d in result.documents],
    "skipped": sorted(result.skipped),
}))
"""

HELDOUT_KEYS = {
    r.partition(":")[2]
    for r, f in mf.parse_manifest(V3_TEXT)[0].member_family.items()
    if mf.parse_manifest(V3_TEXT)[0].family_split[f] == "heldout"
}


def _probe(purpose: str, **dirs: Path) -> dict[str, Any]:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE, purpose, json.dumps({k: str(v) for k, v in dirs.items()})],
        cwd=mf.REPO / "backend",
        capture_output=True,
        check=True,
    )
    out: dict[str, Any] = json.loads(done.stdout)
    return out


def _assert_no_heldout_opened(out: dict[str, Any]) -> None:
    assert len(HELDOUT_KEYS) == 24
    touched = [path for _, path in out["events"]]
    leaked = [p for p in touched if any(k in p for k in HELDOUT_KEYS)]
    assert leaked == [], leaked
    # The corpus index lists every title, held-out ones included: never read it either.
    assert not [p for p in touched if p.endswith("corpus-v3/corpus.toml")]
    assert not [p for kind, p in out["events"] if kind == "list" and "corpus" in p]


def test_training_loads_only_train_and_never_opens_heldout() -> None:
    out = _probe("training")
    _assert_no_heldout_opened(out)
    assert {s for _, s in out["loaded"]} == {"train"} and len(out["loaded"]) == 60
    opened = {p for kind, p in out["events"] if kind == "open"}
    assert sum(p.endswith(".json") and "/corpus-v3/facts/" in p for p in opened) == 60


@pytest.mark.parametrize("purpose", ["tuning", "model-selection"])
def test_tuning_and_model_selection_load_only_dev(purpose: str) -> None:
    out = _probe(purpose)
    _assert_no_heldout_opened(out)
    assert {s for _, s in out["loaded"]} == {"dev"}
    assert len(out["loaded"]) == 17  # 18 dev files minus the scan that needs OCR
    assert "doc:delivery-note-scan" in out["skipped"]
    assert not [p for kind, p in out["events"] if kind == "open" and "/corpus-v3/" in p]


@pytest.mark.parametrize("purpose", ["training", "tuning"])
def test_no_heldout_file_is_opened_when_all_splits_share_one_directory(
    tmp_path: Path, purpose: str
) -> None:
    shared = tmp_path / "all-splits"
    shared.mkdir()
    corpus = M7 / "corpus-v3"
    for path in [*(corpus / "files").iterdir(), *(corpus / "facts").iterdir()]:
        shutil.copy(path, shared / path.name)
    assert len(list(shared.iterdir())) == 168  # 84 documents + 84 sidecars, every split
    out = _probe(purpose, fresh_files_dir=shared, fresh_facts_dir=shared)
    _assert_no_heldout_opened(out)
    opened_shared = {p for kind, p in out["events"] if kind == "open" and str(shared) in p}
    expected = 120 if purpose == "training" else 0  # 60 documents + 60 sidecars
    assert len(opened_shared) == expected
    assert all(Path(p).stem.startswith(("v3-wo-", "v3-sdr-", "v3-pir-")) for p in opened_shared)


def test_no_purpose_can_reach_heldout() -> None:
    assert all("heldout" not in s for s in loader.ALLOWED_SPLITS.values())
    assert set(loader.Purpose) == set(loader.ALLOWED_SPLITS)
    with pytest.raises(ValueError):
        loader.eligible("release-scoring")
    for purpose in loader.Purpose:
        assert not [e for e in loader.eligible(purpose) if e.split == "heldout"]


def test_loader_rejects_a_document_that_differs_from_the_frozen_lock(tmp_path: Path) -> None:
    shared = tmp_path / "train"
    shutil.copytree(M7 / "corpus-v3" / "files", shared)
    (shared / "v3-wo-001.md").write_text("tampered\n")
    with pytest.raises(loader.LoaderError, match="fresh:v3-wo-001 does not match"):
        loader.load("training", fresh_files_dir=shared)


def test_loader_rejects_metadata_that_differs_from_the_reviewed_snapshot(tmp_path: Path) -> None:
    m7 = tmp_path / "m7"
    m7.mkdir()
    for name in ("frozen-snapshots.json", "split-manifest-v3.toml", "split-manifest-v3.lock.json"):
        shutil.copy(M7 / name, m7 / name)
    text = (m7 / "split-manifest-v3.toml").read_text()
    (m7 / "split-manifest-v3.toml").write_text(
        text.replace(
            'id = "fresh-heldout-regenerated-a"\nsplit = "heldout"',
            'id = "fresh-heldout-regenerated-a"\nsplit = "train"',
        )
    )
    with pytest.raises(loader.LoaderError, match="does not match the reviewed snapshot"):
        loader.eligible("training", m7)


@pytest.mark.parametrize(
    "source",
    [
        "docs = sorted(Path('backend/fixtures/evaluation/m7/corpus-v3/files').glob('*.md'))",
        "from app.evaluation.sources import canonical_text, all_file_refs",
        "from app.evaluation import loader, sources",
        "import app.evaluation.corpus_v3 as gen",
    ],
)
def test_a_bulk_reader_that_bypasses_the_loader_fails_the_audit(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    real = mf._artifact_texts()
    monkeypatch.setattr(mf, "_artifact_texts", lambda: (*real, ("ml/train_checker.py", source)))
    problems = mf.audit_exposure(_parse(V3_TEXT))
    assert any(p.startswith("ml/train_checker.py reads M7 sources directly") for p in problems), (
        problems
    )


def test_loader_use_passes_the_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    real = mf._artifact_texts()
    code = "from app.evaluation import loader\ndocs = loader.load('training').documents\n"
    monkeypatch.setattr(mf, "_artifact_texts", lambda: (*real, ("ml/train_checker.py", code)))
    assert mf.audit_exposure(_parse(V3_TEXT)) == []


# ------------------------------------------------------ checker pairs, batch 001
from app.evaluation import checker_pairs as cp  # noqa: E402

_BUILD_PROBE = r"""
import json, sys
events = []
def hook(event, args):
    if event == "open":
        events.append(("open", str(args[0])))
    elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
        events.append(("list", str(args[0])))
sys.addaudithook(hook)
from app.evaluation import checker_pairs
pairs, stats = checker_pairs.build_pairs()
print(json.dumps({"events": events, "splits": sorted({p["split"] for p in pairs}),
                  "sources": sorted({p["source"] for p in pairs} | {p["evidence_source"] for p in pairs})}))
"""


@pytest.fixture(scope="module")
def training() -> loader.LoadResult:
    return loader.load("training")


@pytest.fixture(scope="module")
def batch(training: loader.LoadResult) -> list[dict[str, Any]]:
    pairs, _ = cp.build_pairs(training)
    return pairs


def test_committed_batch_matches_a_fresh_build_and_validates() -> None:
    assert cp.check() == []
    assert cp.token_report_problems() == []


def test_batch_counts_match_the_index(batch: list[dict[str, Any]]) -> None:
    index = json.loads((cp.OUT_DIR / cp.INDEX_FILE).read_text())
    assert index["pairs"] == len(batch) == 2091
    assert index["by_label"] == {"supported": 560, "unsupported": 1531}
    assert index["label_conflicts"] == 0 and index["duplicates_removed"] == 22
    assert index["gap_to_target_lower_bound"] == 3000 - 2091
    assert index["loader_purpose"] == "training" and index["manifest"] == "m7-split-v3"
    assert {p["label_quality"] for p in batch} == {"silver"}


def test_building_the_batch_never_opens_dev_or_heldout_material() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _BUILD_PROBE], cwd=mf.REPO / "backend", capture_output=True,
        check=True,
    )  # fmt: skip
    out = json.loads(done.stdout)
    assert out["splits"] == ["train"]
    assert all(
        s.startswith(("fresh:v3-wo-", "fresh:v3-sdr-", "fresh:v3-pir-")) for s in out["sources"]
    )
    touched = [p for _, p in out["events"]]
    assert not [p for p in touched if any(k in p for k in HELDOUT_KEYS)]
    dev_dirs = ("fixtures/documents/files", "fixtures/legal/files", "fixtures/github-acceptance")
    assert not [p for p in touched if any(d in p for d in dev_dirs)]
    assert not [p for p in touched if p.endswith("corpus-v3/corpus.toml")]
    assert not [p for kind, p in out["events"] if kind == "list" and "fixtures" in p]


def test_only_the_training_loader_can_feed_the_batch() -> None:
    with pytest.raises(ValueError, match="training loader"):
        cp.build_pairs(loader.load("tuning"))


def test_a_dev_or_heldout_document_smuggled_into_the_training_result_is_refused(
    training: loader.LoadResult,
) -> None:
    for split, ref, family in (
        ("dev", "doc:hr-leave-policy", "hr-leave-policy"),
        ("heldout", "fresh:v3-ps-001", "fresh-heldout-regenerated-a"),
    ):
        smuggled = loader.LoadedDocument(ref, family, split, "Title\n\nText.", {"reference": "X"})
        tainted = loader.LoadResult(
            training.purpose, training.manifest, [*training.documents, smuggled]
        )
        with pytest.raises(ValueError, match=f"{ref} is not a train document"):
            cp.build_pairs(tainted)


@pytest.mark.parametrize(
    ("ref", "family", "split"),
    [
        ("doc:hr-leave-policy", "hr-leave-policy", "dev"),
        ("fresh:v3-ps-001", "fresh-heldout-regenerated-a", "heldout"),
    ],
)
def test_validation_rejects_dev_and_heldout_pairs(
    training: loader.LoadResult, batch: list[dict[str, Any]], ref: str, family: str, split: str
) -> None:
    pairs = copy.deepcopy(batch)
    pairs[0].update(source=ref, evidence_source=ref, family=family, split=split)
    problems = cp.validate(pairs, training)
    assert any(f"{ref} is not a train document" in p for p in problems)
    assert any(f"split {split} / family {family} is not train" in p for p in problems)


def test_every_variant_keeps_its_parents_family_split_and_source(
    training: loader.LoadResult, batch: list[dict[str, Any]]
) -> None:
    by_id = {p["id"]: p for p in batch}
    for p in batch:
        if p["parent"] is None:
            assert p["variant"] == "original" and p["label"] == "supported"
            continue
        parent = by_id[p["parent"]]
        assert (parent["family"], parent["split"], parent["source"]) == (
            p["family"], p["split"], p["source"]
        )  # fmt: skip
        assert p["label"] == "unsupported"
    pairs = copy.deepcopy(batch)
    variant = next(p for p in pairs if p["variant"] == "swap")
    variant["source"] = next(p["source"] for p in pairs if p["source"] != variant["source"])
    assert any("variant left its parent" in p for p in cp.validate(pairs, training))


def test_wrong_evidence_stays_in_family_and_is_another_document(
    batch: list[dict[str, Any]],
) -> None:
    wrong = [p for p in batch if p["variant"] == "wrong_evidence"]
    assert len(wrong) == 560
    assert all(p["evidence_source"] != p["source"] for p in wrong)
    assert all(p["evidence_source"].split("-")[1] == p["source"].split("-")[1] for p in wrong)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p.update(evidence_start=p["evidence_start"] + 1), "not the exact span"),
        (lambda p: p.update(premise=p["evidence"]), "premise is not title + evidence"),
        (lambda p: p.update(claim="It is late; the fee rose."), "not atomic"),
        (lambda p: p.update(label="supported"), "only the original claim may be supported"),
        (lambda p: p.update(label_quality="gold"), "labels must be silver"),
    ],
)
def test_validation_catches_broken_pairs(
    training: loader.LoadResult, batch: list[dict[str, Any]], edit: Any, message: str
) -> None:
    pairs = copy.deepcopy(batch)
    edit(next(p for p in pairs if p["variant"] == "shift"))
    assert any(message in p for p in cp.validate(pairs, training))


def test_every_evidence_span_is_exact_and_claims_are_atomic(
    training: loader.LoadResult, batch: list[dict[str, Any]]
) -> None:
    texts = {d.ref: d.text for d in training.documents}
    for p in batch:
        assert texts[p["evidence_source"]][p["evidence_start"] : p["evidence_end"]] == p["evidence"]
        assert p["claim"].count(". ") == 0 and ";" not in p["claim"]


def test_bound_claims_are_only_perturbed_to_stricter_values(batch: list[dict[str, Any]]) -> None:
    def hours(text: str) -> int:
        return int(re.search(r"within (\d+) hours", text).group(1))  # type: ignore[union-attr]

    for p in batch:
        if p["claim_type"] == "response" and p["variant"] in ("swap", "shift"):
            assert hours(p["claim"]) < hours(p["evidence"])


def test_batch_build_is_deterministic(training: loader.LoadResult) -> None:
    assert cp.render(training) == cp.render(training)


def test_train_claims_share_no_text_with_dev_pilot_records(batch: list[dict[str, Any]]) -> None:
    from app.evaluation.checks import load_data, normalise

    dev = {
        normalise(t)
        for recs in load_data(freeze.disk_reader()).values()
        for r in recs
        for t in [r.get("claim", ""), r.get("question", ""), r.get("query", "")]
        if t
    }
    assert not {normalise(p["claim"]) for p in batch} & dev


def test_token_report_matches_the_pins_and_has_no_over_length_pairs() -> None:
    report = json.loads((cp.OUT_DIR / cp.LENGTHS_FILE).read_text())
    assert report["tokenizer"]["revision"] == "6f5cf0a2b59cabb106aca4c287eed12e357e90eb"
    assert report["tokenizer"]["class"] == "DebertaV2TokenizerFast"
    assert report["overall"]["over_limit"] == 0 and report["over_limit_ids"] == []
    assert report["overall"]["max"] <= report["limit"] == 512


def test_token_report_for_other_pairs_is_rejected(tmp_path: Path) -> None:
    for name in (cp.PAIRS_FILE, cp.LENGTHS_FILE):
        shutil.copy(cp.OUT_DIR / name, tmp_path / name)
    lines = (tmp_path / cp.PAIRS_FILE).read_text().splitlines(keepends=True)
    (tmp_path / cp.PAIRS_FILE).write_text("".join(lines[:-1]))
    problems = cp.token_report_problems(tmp_path)
    assert any("measured on different pairs" in p for p in problems)
    assert any("does not cover exactly" in p for p in problems)


# --------------------------------------- batch 001 re-review: ambiguity and pinning
def _decision_status(text: str) -> str:
    return re.search(r'as exactly "([^"]+)"', text).group(1)  # type: ignore[union-attr]


def test_decision_claims_state_an_exact_status(batch: list[dict[str, Any]]) -> None:
    decisions = [p for p in batch if p["claim_type"] == "decision"]
    assert decisions
    for p in decisions:
        assert re.search(r'records the decision status for .+ as exactly "[a-z ]+"\.$', p["claim"])
        evidence_status = re.search(r"The supplier is (.+)\.", p["evidence"]).group(1)  # type: ignore[union-attr]
        same = _decision_status(p["claim"]) == evidence_status
        if p["variant"] != "wrong_evidence":
            assert same == (p["label"] == "supported"), p["id"]
    # The formerly ambiguous case: "approved" against "approved with conditions" is now an
    # explicit status mismatch, so "unsupported" is unambiguous.
    flagged = [
        p for p in decisions
        if p["variant"] != "wrong_evidence" and "approved with conditions" in p["evidence"]
        and _decision_status(p["claim"]) == "approved"
    ]  # fmt: skip
    assert len(flagged) == 3 and {p["label"] for p in flagged} == {"unsupported"}
    assert not [p for p in batch if p["claim"].endswith(" as approved.")]


def test_start_year_claims_state_the_exact_start_year(batch: list[dict[str, Any]]) -> None:
    years = [p for p in batch if p["claim_type"] == "start-year"]
    assert years and not [p for p in batch if " since " in p["claim"]]
    later = 0
    for p in years:
        claim_year = int(re.search(r"started using .+ in (\d{4})\.$", p["claim"]).group(1))  # type: ignore[union-attr]
        evidence_year = int(re.search(r"used since (\d{4})", p["evidence"]).group(1))  # type: ignore[union-attr]
        if p["variant"] != "wrong_evidence":
            assert (claim_year == evidence_year) == (p["label"] == "supported"), p["id"]
            later += claim_year > evidence_year
    assert later > 0  # later-year variants exist and are explicit mismatches, so unsupported


def test_count_claims_state_totals(batch: list[dict[str, Any]]) -> None:
    for kind in ("labour", "accounts", "duration"):
        claims = [p["claim"] for p in batch if p["claim_type"] == kind]
        assert claims and all(c.endswith(" in total.") for c in claims)


def test_still_entailed_rules() -> None:
    specs = {s.name: s for s in cp.SPECS["fresh-train-regenerated"]}
    response, labour = specs["response"], specs["labour"]
    assert cp.still_entailed(response, 24, 72)  # a looser bound is still supported
    assert not cp.still_entailed(response, 24, 4)
    assert not cp.still_entailed(labour, 16, 10) and cp.still_entailed(labour, 16, 16)


@pytest.mark.parametrize("claim_type", ["decision", "start-year", "response"])
def test_validation_rejects_unsupported_variants_the_evidence_still_entails(
    training: loader.LoadResult, batch: list[dict[str, Any]], claim_type: str
) -> None:
    pairs = copy.deepcopy(batch)
    p = next(
        p for p in pairs
        if p["claim_type"] == claim_type and p["variant"] in ("swap", "shift")
    )  # fmt: skip
    looser = p["evidence_value"] + 1 if claim_type == "response" else p["evidence_value"]
    p["claim_value"] = looser
    assert any("its evidence still entails it" in x for x in cp.validate(pairs, training))


def _activate_newer_manifest(tmp_path: Path) -> Path:
    """A copy of the M7 metadata in which a v4 manifest (moving a train family to
    held-out) is active and v3 is superseded."""

    m7 = tmp_path / "m7"
    m7.mkdir()
    for name in ("frozen-snapshots.json", "split-manifest-v3.toml", "split-manifest-v3.lock.json"):
        shutil.copy(M7 / name, m7 / name)
    v4_text = V3_TEXT.replace(
        'version = 3\nname = "m7-split-v3"', 'version = 4\nname = "m7-split-v4"'
    ).replace(
        'id = "fresh-train-incident-review"\nsplit = "train"',
        'id = "fresh-train-incident-review"\nsplit = "heldout"',
    )
    v4 = _parse(v4_text)
    lock_text = mf.lock_text(mf.build_lock(v4, "split-manifest-v4.toml"))
    (m7 / "split-manifest-v4.toml").write_text(v4_text)
    (m7 / "split-manifest-v4.lock.json").write_text(lock_text)
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    snaps["manifests"]["m7-split-v3"].update(
        status="superseded", superseded_by="m7-split-v4", reason="test"
    )
    snaps["manifests"]["m7-split-v4"] = {
        "manifest": "split-manifest-v4.toml",
        "lock": "split-manifest-v4.lock.json",
        "manifest_sha256": freeze._sha(v4_text),
        "lock_sha256": freeze._sha(lock_text),
        "frozen_at": "2026-12-01",
        "status": "active",
    }
    snaps["active"] = "m7-split-v4"
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    return m7


def test_batch_001_rebuilds_byte_for_byte_after_a_newer_manifest_is_activated(
    tmp_path: Path,
) -> None:
    m7 = _activate_newer_manifest(tmp_path)
    active = loader.load("training", m7_dir=m7)  # unpinned: follows the newer manifest
    assert active.manifest == "m7-split-v4" and len(active.documents) == 40
    pinned = cp.load_pinned(m7)
    assert pinned.manifest == "m7-split-v3" and len(pinned.documents) == 60
    for name, text in cp.render(pinned).items():
        assert text == (cp.OUT_DIR / name).read_text("utf-8"), name
    assert cp.check(m7_dir=m7) == []
    with pytest.raises(ValueError, match="must be built from m7-split-v3"):
        cp.render(active)


def test_pinned_load_rejects_a_snapshot_entry_that_differs_from_the_pin(tmp_path: Path) -> None:
    m7 = _activate_newer_manifest(tmp_path)
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    snaps["manifests"]["m7-split-v3"]["lock_sha256"] = "0" * 64
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    with pytest.raises(loader.LoaderError, match="differs from the pin"):
        cp.load_pinned(m7)


def test_index_records_the_pinned_manifest_hashes() -> None:
    index = json.loads((cp.OUT_DIR / cp.INDEX_FILE).read_text())
    assert (index["manifest"], index["manifest_sha256"], index["lock_sha256"]) == (
        cp.PIN.name, cp.PIN.manifest_sha256, cp.PIN.lock_sha256
    )  # fmt: skip
    snaps = json.loads((M7 / "frozen-snapshots.json").read_text())
    entry = snaps["manifests"][cp.PIN.name]
    assert (entry["manifest_sha256"], entry["lock_sha256"]) == (
        cp.PIN.manifest_sha256, cp.PIN.lock_sha256
    )  # fmt: skip


# ----------------------------------------------------------- checker baseline runner
from app.evaluation import checker_baselines as cb  # noqa: E402
from app.evaluation import checker_metrics as cm  # noqa: E402
from app.reporting.model_client import ChatResult, CheckResult, FakeModelClient  # noqa: E402

S, U = cm.SUPPORTED, cm.UNSUPPORTED


def _synthetic_rows() -> list[cm.Scored]:
    scorer = cb.SyntheticScorer()
    out = []
    for pair in cb.synthetic_pairs():
        p, label, ms, _ = scorer.score(pair)
        out.append(cm.Scored(pair.pair_id, pair.gold, p, label, ms, pair.change_type))
    return out


def test_unsupported_precision_recall_f1_and_false_support_rate_by_hand() -> None:
    rows = _synthetic_rows()
    assert cm.confusion(rows) == {"tp": 5, "fp": 1, "fn": 2, "tn": 4}
    prf = cm.unsupported_prf(rows)
    assert prf["precision"] == pytest.approx(5 / 6)
    assert prf["recall"] == pytest.approx(5 / 7)
    assert prf["f1"] == pytest.approx(2 * (5 / 6) * (5 / 7) / (5 / 6 + 5 / 7))
    fsr = cm.false_support_rate(rows)
    assert fsr["rate"] == pytest.approx(2 / 7)
    assert (fsr["passed_wrong_claims"], fsr["gold_unsupported"]) == (2, 7)
    assert fsr["wilson95"] == pytest.approx([0.0822, 0.6411], abs=1e-4)


def test_calibration_by_hand() -> None:
    cal = cm.calibration(_synthetic_rows())
    # Ten equal-width bins; gaps .05, 2x.125, .2, .7, .4, .55, .6, .3, .2, 2x.075 over 12.
    assert cal["ece"] == pytest.approx(3.4 / 12)
    assert cal["mce"] == pytest.approx(0.7)
    golds = [1] * 5 + [0] * 7
    expected_brier = (
        sum((p - g) ** 2 for p, g in zip(cb.SyntheticScorer.P, golds, strict=True)) / 12
    )
    assert cal["brier"] == pytest.approx(expected_brier)
    assert sum(b["n"] for b in cal["bins"]) == 12 and cal["hard_labels"] is False
    hard = [cm.Scored("a", S, 1.0, S, 1, "x"), cm.Scored("b", U, 1.0, S, 1, "x")]
    hard_cal = cm.calibration(hard)
    assert hard_cal["hard_labels"] is True and hard_cal["ece"] == pytest.approx(0.5)


def test_accuracy_per_change_type_and_latency_by_hand() -> None:
    rows = _synthetic_rows()
    acc = {k: v["accuracy"] for k, v in cm.accuracy_by_change_type(rows).items()}
    assert acc == {"entity": 1.0, "number": 0.5, "original": 0.8, "polarity": 0.0,
                   "scope": 1.0, "wrong_evidence": 1.0}  # fmt: skip
    lat = cm.latency(rows)
    assert (lat["p50_ms"], lat["p95_ms"], lat["mean_ms"], lat["max_ms"]) == (11.0, 50.0, 16.0, 50.0)
    assert cm.nearest_rank([3.0, 1.0, 2.0], 0.5) == 2.0 and cm.nearest_rank([], 0.5) is None


def test_undefined_metrics_are_none_never_perfect() -> None:
    only_supported = [cm.Scored("a", S, 0.9, S, 1, "x"), cm.Scored("b", S, 0.8, S, 1, "x")]
    prf = cm.unsupported_prf(only_supported)
    assert prf == {"precision": None, "recall": None, "f1": None}
    assert cm.false_support_rate(only_supported)["rate"] is None
    all_wrong = [cm.Scored("a", S, 0.1, U, 1, "x"), cm.Scored("b", U, 0.9, S, 1, "x")]
    assert cm.unsupported_prf(all_wrong) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    assert cm.wilson(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert cm.wilson(0, 0) is None


@pytest.mark.parametrize(
    "row",
    [
        cm.Scored("a", "maybe", 0.5, S, 1, "x"),
        cm.Scored("a", S, 0.5, "maybe", 1, "x"),
        cm.Scored("a", S, 1.5, S, 1, "x"),
    ],
)
def test_summarize_rejects_invalid_rows(row: cm.Scored) -> None:
    with pytest.raises(ValueError):
        cm.summarize([row])


def test_scoring_is_blocked_without_human_dev_labels_and_never_creates_a_scorer(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "no-labels"
    empty.mkdir()
    pairs, dataset = cb.dev_eval_set(empty)
    assert pairs == [] and dataset["candidate_pairs"] == 37
    assert dataset["human_reviewed_pairs"] == 0

    def must_not_be_called() -> cb.Scorer:
        raise AssertionError("the model gateway must not be contacted when blocked")

    report = cb.run(must_not_be_called, pairs, dataset)
    assert report["status"] == "blocked" and "0 of 37" in report["reason"]
    assert "metrics" not in report and "scorer" not in report


def test_cli_reports_blocked_with_a_distinct_exit_code(tmp_path: Path) -> None:
    from app.evaluation.__main__ import main

    empty = tmp_path / "no-labels"
    empty.mkdir()
    argv = ["checker-baselines", "--scorer", "deberta", "--label-dir", str(empty)]
    assert main(argv) == cb.EXIT_BLOCKED


def _write_human_labels(label_dir: Path, decide: Callable[[dict[str, Any]], str]) -> None:
    label_dir.mkdir(parents=True, exist_ok=True)
    rev = lab.dataset_revision()
    for name in ("ann-a", "ann-b"):
        with (label_dir / f"gold-{name}.csv").open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=lab.LABEL_COLUMNS)
            writer.writeheader()
            for pair in lab.gold_pairs():
                writer.writerow({"pair_id": pair["id"], "label": decide(pair), "note": "",
                                 "dataset_revision": rev})  # fmt: skip


def test_eval_set_uses_human_labels_only_never_proposals(tmp_path: Path) -> None:
    # Test-only synthetic "human" labels: the opposite of every AI proposal.
    flip = {S: U, U: S}
    _write_human_labels(tmp_path, lambda p: flip[p["proposed_label"]])
    pairs, dataset = cb.dev_eval_set(tmp_path)
    proposals = {p["id"]: p["proposed_label"] for p in lab.gold_pairs()}
    assert len(pairs) == dataset["human_reviewed_pairs"] == 37
    assert all(p.gold == flip[proposals[p.pair_id]] for p in pairs)
    assert dataset["label_files_sha256"].keys() == {"gold-ann-a.csv", "gold-ann-b.csv"}


def test_single_labels_and_open_disagreements_are_never_scored(tmp_path: Path) -> None:
    ids = [p["id"] for p in lab.gold_pairs()]
    rev = lab.dataset_revision()
    _write_rows(tmp_path / "gold-ann-a.csv", [
        {"pair_id": ids[0], "label": "supported", "note": "", "dataset_revision": rev},
        {"pair_id": ids[1], "label": "supported", "note": "", "dataset_revision": rev},
        {"pair_id": ids[2], "label": "supported", "note": "", "dataset_revision": rev},
    ])  # fmt: skip
    _write_rows(tmp_path / "gold-ann-b.csv", [
        {"pair_id": ids[0], "label": "supported", "note": "", "dataset_revision": rev},
        {"pair_id": ids[1], "label": "unsupported", "note": "", "dataset_revision": rev},
    ])  # fmt: skip
    pairs, _ = cb.dev_eval_set(tmp_path)
    assert [p.pair_id for p in pairs] == [ids[0]]  # agreed only; ids[1] pending, ids[2] single


@pytest.mark.parametrize(
    ("split", "sources", "message"),
    [
        ("train", None, "not a dev pilot pair"),
        ("dev", ["fresh:v3-wo-001"], "undeclared evidence|cites non-dev sources"),
        ("dev", ["fresh:v3-ps-001"], "undeclared evidence|cites non-dev sources"),
    ],
)
def test_eval_set_refuses_non_dev_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, split: str, sources: Any, message: str
) -> None:
    real = lab.gold_pairs()
    tainted = copy.deepcopy(real)
    tainted[0]["split"] = split
    if sources:
        tainted[0]["sources"] = sources
    monkeypatch.setattr(lab, "gold_pairs", lambda: tainted)
    with pytest.raises(cb.SplitViolation, match=message):
        cb.dev_eval_set(tmp_path)


def test_eval_set_follows_the_frozen_dev_selection(tmp_path: Path) -> None:
    m7 = tmp_path / "m7"
    m7.mkdir()
    for name in ("frozen-snapshots.json", "split-manifest-v3.toml", "split-manifest-v3.lock.json"):
        shutil.copy(M7 / name, m7 / name)
    v4_text = V3_TEXT.replace(
        'version = 3\nname = "m7-split-v3"', 'version = 4\nname = "m7-split-v4"'
    ).replace('id = "hr-leave-policy"\nsplit = "dev"', 'id = "hr-leave-policy"\nsplit = "heldout"')
    v4 = mf.parse_manifest(v4_text)[0]
    lock_text = mf.lock_text(mf.build_lock(v4, "split-manifest-v4.toml"))
    (m7 / "split-manifest-v4.toml").write_text(v4_text)
    (m7 / "split-manifest-v4.lock.json").write_text(lock_text)
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    snaps["manifests"]["m7-split-v4"] = {
        "manifest": "split-manifest-v4.toml", "lock": "split-manifest-v4.lock.json",
        "manifest_sha256": freeze._sha(v4_text), "lock_sha256": freeze._sha(lock_text),
        "frozen_at": "2026-12-01", "status": "active",
    }  # fmt: skip
    snaps["active"] = "m7-split-v4"
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    with pytest.raises(cb.SplitViolation, match="doc:hr-leave-policy"):
        cb.dev_eval_set(tmp_path / "labels", m7)


_EVAL_PROBE = r"""
import json, sys
events = []
sys.addaudithook(lambda e, a: events.append(str(a[0])) if e in ("open", "os.listdir", "os.scandir") else None)
from pathlib import Path
from app.evaluation import checker_baselines as cb
pairs, dataset = cb.dev_eval_set(Path(sys.argv[1]))
report = cb.run(lambda: None, pairs, dataset)
print(json.dumps({"events": events, "status": report["status"]}))
"""


def test_the_runner_never_opens_train_heldout_or_corpus_files(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _EVAL_PROBE, str(tmp_path)], cwd=mf.REPO / "backend",
        capture_output=True,
        check=True,
    )  # fmt: skip
    out = json.loads(done.stdout)
    assert out["status"] == "blocked"
    touched = out["events"]
    assert not [p for p in touched if "corpus-v3" in p or "/checker/" in p]
    assert not [p for p in touched if any(k in p for k in HELDOUT_KEYS)]
    assert not [p for p in touched if "blind" in p.lower()]


def test_deberta_scorer_uses_the_gateway_check_at_the_configured_threshold() -> None:
    client = FakeModelClient(checker=lambda premise, claim: CheckResult(0.62, 0.3, 0.08))
    scorer = cb.DebertaGatewayScorer(client, threshold=0.7)
    pair = cb.synthetic_pairs()[0]
    p, label, ms, extra = scorer.score(pair)
    assert (p, label) == (0.62, U) and ms >= 0 and extra["contradiction"] == 0.08
    desc = scorer.describe()
    assert desc["operating_point"]["threshold"] == 0.7
    assert "not selected" in desc["operating_point"]["provenance"]


class _JudgeStub:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers

    def describe(self) -> dict[str, Any]:
        return {"chat": {"model": "Qwen/Qwen3-8B-AWQ", "revision": "pinned"}}

    def served_models(self) -> Any:
        return type("S", (), {"summary": lambda self: {"stub": True}})()

    def chat(self, messages: list[dict[str, str]], **_: Any) -> ChatResult:
        return ChatResult(self.answers.pop(0), "stop", 1, 0.01, 0.02)


@pytest.mark.parametrize(
    ("answer", "label", "unparseable"),
    [("SUPPORTED", S, False), ("Unsupported.", U, False), ("NOT supported", U, False),
     ("maybe", U, True), ("", U, True)],
)  # fmt: skip
def test_qwen_judge_parses_one_word_answers_and_fails_closed(
    answer: str, label: str, unparseable: bool
) -> None:
    scorer = cb.QwenJudgeScorer(_JudgeStub([answer]))  # type: ignore[arg-type]
    p, got, _, extra = scorer.score(cb.synthetic_pairs()[0])
    assert (got, extra["unparseable"], p) == (label, unparseable, 1.0 if label == S else 0.0)


def test_synthetic_selftest_is_marked_and_matches_the_hand_computed_metrics() -> None:
    report = cb.synthetic_selftest()
    assert (
        report["status"] == "synthetic-selftest" and "no model was scored" in report["not_a_result"]
    )
    m = report["metrics"]
    assert m["unsupported"]["recall"] == pytest.approx(5 / 7)
    assert m["false_support_rate"]["rate"] == pytest.approx(2 / 7)
    assert m["calibration"]["ece"] == pytest.approx(3.4 / 12)
    assert len(report["predictions"]) == 12 and report["runtime"]["python"]


# ------------------------------------------------- baseline runner re-review (PR #13)
import httpx  # noqa: E402

from app.config import Settings  # noqa: E402
from app.reporting.model_client import GatewayModelClient  # noqa: E402

_PINNED_HEALTH: dict[str, Any] = {
    "models": {
        "chat": {
            "model": "Qwen/Qwen3-8B-AWQ",
            "revision": "4da05a8edb55c6046cce958586c33b61da07bb79",
        },
        "checker": {
            "model": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
            "revision": "6f5cf0a2b59cabb106aca4c287eed12e357e90eb",
        },
    },
    "engine": {"gpu_memory_utilization": 0.75, "max_model_len": 4096},
}


def _gateway_settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+psycopg://x@localhost/none",
        REDIS_URL="redis://localhost:6379/0",
        QDRANT_URL="http://localhost:6333",
        S3_ENDPOINT_URL="http://localhost:8333",
        JWT_SECRET="unit-test-secret-unit-test-secret-0000",
        LLM_BASE_URL="https://gpu-gateway.example.test/eaios",
        MODEL_GATEWAY_TOKEN="service-token-for-tests-only",
        MODEL_MODE="gateway",
    )


class _MockGateway:
    """An in-process gateway (httpx.MockTransport): no network, records every request."""

    def __init__(self, health: dict[str, Any]) -> None:
        self.health = health
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/eaios")
        self.paths.append(path)
        if path == "/health":
            return httpx.Response(200, json=self.health)
        if path == "/check":
            n = len(json.loads(request.content)["pairs"])
            return httpx.Response(
                200,
                json={"results": [{"entailment": 0.7, "neutral": 0.2, "contradiction": 0.1}] * n},
            )
        return httpx.Response(404)

    def client(self) -> GatewayModelClient:
        return GatewayModelClient(_gateway_settings(), transport=httpx.MockTransport(self))


@pytest.fixture
def agreed(tmp_path: Path) -> Path:
    """Test-only agreeing labels from two annotators (the AI proposals, copied)."""

    label_dir = tmp_path / "labels"
    _write_human_labels(label_dir, lambda p: p["proposed_label"])
    return label_dir


# 1. fake inference is rejected before any example is sent
def test_fake_inference_is_rejected_before_any_example(agreed: Path) -> None:
    pairs, dataset = cb.dev_eval_set(agreed)
    calls: list[str] = []

    def checker(premise: str, claim: str) -> CheckResult:
        calls.append(claim)
        return CheckResult(0.9, 0.05, 0.05)

    fake = FakeModelClient(checker=checker)
    with pytest.raises(cb.BackendRejected, match="MODEL_MODE=gateway, got fake"):
        cb.run(lambda: cb.DebertaGatewayScorer(fake, 0.5), pairs, dataset)
    with pytest.raises(cb.BackendRejected, match="got fake"):
        cb.run(lambda: cb.QwenJudgeScorer(fake), pairs, dataset)
    with pytest.raises(cb.BackendRejected, match="only for --synthetic-selftest"):
        cb.run(cb.SyntheticScorer, pairs, dataset)
    assert calls == []


@pytest.mark.parametrize(
    "health",
    [
        {**_PINNED_HEALTH, "models": {"chat": _PINNED_HEALTH["models"]["chat"]}},  # no checker
        {**_PINNED_HEALTH, "models": {**_PINNED_HEALTH["models"],
                                      "checker": {"model": "other/nli", "revision": "abc"}}},
    ],
)  # fmt: skip
def test_wrong_or_missing_served_checker_is_rejected_before_any_example(
    agreed: Path, health: dict[str, Any]
) -> None:
    pairs, dataset = cb.dev_eval_set(agreed)
    gateway = _MockGateway(health)
    with pytest.raises(cb.BackendRejected):
        cb.run(lambda: cb.DebertaGatewayScorer(gateway.client(), 0.5), pairs, dataset)
    assert "/check" not in gateway.paths


def test_cli_rejects_fake_mode_with_its_own_exit_code(
    agreed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.evaluation import __main__ as cli

    monkeypatch.setenv("MODEL_MODE", "fake")
    assert cli.main(["checker-baselines", "--scorer", "deberta", "--label-dir", str(agreed)]) == 4


# 5. agreed labels reach the scoring path, with no real gateway
def test_agreed_labels_are_scored_through_the_gateway_path_without_a_real_gateway(
    agreed: Path,
) -> None:
    pairs, dataset = cb.dev_eval_set(agreed)
    assert len(pairs) == 37
    gateway = _MockGateway(_PINNED_HEALTH)
    report = cb.run(lambda: cb.DebertaGatewayScorer(gateway.client(), 0.5), pairs, dataset)
    assert report["status"] == "scored" and report["metrics"]["pairs"] == 37
    assert gateway.paths[0] == "/health" and gateway.paths.count("/check") == 37
    assert report["scorer"]["inference"] == "gateway"
    assert report["scorer"]["served"]["models"]["checker"]["revision"].startswith("6f5cf0a2")


def test_cli_scores_agreed_labels_through_a_mock_gateway(
    agreed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.evaluation import __main__ as cli

    gateway = _MockGateway(_PINNED_HEALTH)
    monkeypatch.setattr(cli, "build_model_client", lambda settings: gateway.client())
    out = tmp_path / "report.json"
    argv = [
        "checker-baselines",
        "--scorer",
        "deberta",
        "--label-dir",
        str(agreed),
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 0
    report = json.loads(out.read_text())
    assert report["status"] == "scored" and report["dataset"]["human_reviewed_pairs"] == 37
    assert gateway.paths.count("/check") == 37


# 2. evidence references
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evidence_source", "fresh:v3-ps-001", "undeclared evidence ['fresh:v3-ps-001']"),
        ("claim_about", "fresh:v3-wo-001", "undeclared evidence ['fresh:v3-wo-001']"),
        ("evidence_source", "doc:it-security-handbook", "undeclared evidence"),
    ],
)
def test_eval_set_validates_actual_evidence_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str, message: str
) -> None:
    tainted = copy.deepcopy(lab.gold_pairs())
    tainted[0][field] = value  # `sources` still lists only valid dev documents
    monkeypatch.setattr(lab, "gold_pairs", lambda: tainted)
    with pytest.raises(cb.SplitViolation, match=re.escape(message)):
        cb.dev_eval_set(tmp_path)


def test_declared_heldout_evidence_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tainted = copy.deepcopy(lab.gold_pairs())
    tainted[0]["evidence_source"] = "fresh:v3-ps-001"
    tainted[0]["sources"] = sorted({*tainted[0]["sources"], "fresh:v3-ps-001"})
    monkeypatch.setattr(lab, "gold_pairs", lambda: tainted)
    with pytest.raises(cb.SplitViolation, match="cites non-dev sources"):
        cb.dev_eval_set(tmp_path)


# 3. predictions bound to exact inputs
def _predictions(path: Path, export: str, header: dict[str, Any] | None = None) -> Path:
    lines = [json.loads(x) for x in export.splitlines()]
    head = {"model": "vectara/hallucination_evaluation_model", "revision": "abc",
            "runtime": {"gpu": "T4"}, "inputs_sha256": lines[0]["inputs_sha256"], **(header or {})}  # fmt: skip
    rows = [{"pair_id": r["pair_id"], "input_sha256": r["input_sha256"], "p_supported": 0.4,
             "predicted": U, "latency_ms": 7} for r in lines[1:]]  # fmt: skip
    path.write_text("\n".join(json.dumps(x) for x in [head, *rows]))
    return path


def test_predictions_matching_the_exported_inputs_are_accepted(
    agreed: Path, tmp_path: Path
) -> None:
    pairs, dataset = cb.dev_eval_set(agreed)
    path = _predictions(tmp_path / "p.jsonl", cb.export_inputs(pairs))
    report = cb.run(lambda: cb.PredictionsScorer(path, pairs), pairs, dataset)
    assert report["status"] == "scored" and report["scorer"]["inputs_sha256"] == cb.inputs_digest(
        pairs
    )


def test_stale_predictions_for_changed_inputs_under_the_same_ids_are_rejected(
    agreed: Path, tmp_path: Path
) -> None:
    pairs, _ = cb.dev_eval_set(agreed)
    path = _predictions(tmp_path / "p.jsonl", cb.export_inputs(pairs))
    changed = list(pairs)
    changed[0] = cb.EvalPair(pairs[0].pair_id, pairs[0].claim + " Edited.", pairs[0].premise,
                             pairs[0].gold, pairs[0].change_type, pairs[0].family)  # fmt: skip
    with pytest.raises(ValueError, match="inputs_sha256 is stale"):
        cb.PredictionsScorer(path, changed)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ({"inputs_sha256": "0" * 64}, "inputs_sha256 is stale"),
        ({"inputs_sha256": ""}, "header lacks"),
        ({"revision": ""}, "header lacks"),
    ],
)
def test_predictions_header_must_bind_model_and_inputs(
    agreed: Path, tmp_path: Path, header: dict[str, Any], message: str
) -> None:
    pairs, _ = cb.dev_eval_set(agreed)
    path = _predictions(tmp_path / "p.jsonl", cb.export_inputs(pairs), header)
    with pytest.raises(ValueError, match=message):
        cb.PredictionsScorer(path, pairs)


def test_per_pair_input_digests_are_required(agreed: Path, tmp_path: Path) -> None:
    pairs, _ = cb.dev_eval_set(agreed)
    path = _predictions(tmp_path / "p.jsonl", cb.export_inputs(pairs))
    lines = path.read_text().splitlines()
    row = json.loads(lines[1])
    row["input_sha256"] = cb.input_digest(row["pair_id"], "a different claim", "a premise")
    path.write_text("\n".join([lines[0], json.dumps(row), *lines[2:]]))
    with pytest.raises(ValueError, match="stale or missing input_sha256"):
        cb.PredictionsScorer(path, pairs)


def test_predictions_from_fake_inference_are_rejected(agreed: Path, tmp_path: Path) -> None:
    pairs, _ = cb.dev_eval_set(agreed)
    path = _predictions(tmp_path / "p.jsonl", cb.export_inputs(pairs), {"inference": "fake"})
    with pytest.raises(cb.BackendRejected, match="fake inference"):
        cb.PredictionsScorer(path, pairs)


def test_cli_exports_inputs_with_digests(agreed: Path, tmp_path: Path) -> None:
    from app.evaluation.__main__ import main

    out = tmp_path / "inputs.jsonl"
    assert main(["checker-baselines", "--export-inputs", str(out), "--label-dir", str(agreed)]) == 0
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert lines[0]["pairs"] == 37 and len(lines) == 38
    assert all(r["input_sha256"] == cb.input_digest(r["pair_id"], r["claim"], r["premise"])
               for r in lines[1:])  # fmt: skip


# 4. unsupported-class F1
def test_f1_is_zero_when_every_unsupported_claim_is_missed() -> None:
    rows = [cm.Scored("a", U, 0.9, S, 1, "x"), cm.Scored("b", U, 0.8, S, 1, "x"),
            cm.Scored("c", S, 0.9, S, 1, "x")]  # fmt: skip
    prf = cm.unsupported_prf(rows)
    assert prf == {"precision": None, "recall": 0.0, "f1": 0.0}


def test_f1_uses_two_tp_over_two_tp_plus_fp_plus_fn() -> None:
    c = cm.confusion(_synthetic_rows())
    expected = 2 * c["tp"] / (2 * c["tp"] + c["fp"] + c["fn"])
    assert (
        cm.unsupported_prf(_synthetic_rows())["f1"]
        == pytest.approx(expected)
        == pytest.approx(10 / 13)
    )
    only_supported = [cm.Scored("a", S, 0.9, S, 1, "x")]
    assert cm.unsupported_prf(only_supported)["f1"] is None  # denominator zero
    over_flag = [cm.Scored("a", S, 0.1, U, 1, "x")]  # TP = 0, FP = 1: F1 is 0, not None
    assert cm.unsupported_prf(over_flag)["f1"] == 0.0


# ---------------------------------------------------------------- HHEM-2.1-open scorer
import importlib.util  # noqa: E402

_HHEM_PATH = mf.REPO / "ml" / "hhem-baseline" / "hhem_score.py"


def _load_hhem() -> Any:
    spec = importlib.util.spec_from_file_location("hhem_score", _HHEM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hhem_score"] = module
    spec.loader.exec_module(module)
    return module


hhem = _load_hhem()


def _export(agreed: Path, tmp_path: Path) -> tuple[list[cb.EvalPair], dict[str, Any], Path]:
    pairs, dataset = cb.dev_eval_set(agreed)
    path = tmp_path / "inputs.jsonl"
    path.write_text(cb.export_inputs(pairs), "utf-8")
    return pairs, dataset, path


def test_hhem_digests_match_the_runner() -> None:
    for pid, claim, premise in [("a", "b", "c"), ("ü-1", "Zürich 🚀 claim.", "Title\n\nSpan.")]:
        assert hhem.input_digest(pid, claim, premise) == cb.input_digest(pid, claim, premise)
    pairs = cb.synthetic_pairs()
    rows = [{"pair_id": p.pair_id, "claim": p.claim, "premise": p.premise} for p in pairs]
    assert hhem.inputs_digest(rows) == cb.inputs_digest(pairs)


def test_hhem_round_trip_is_accepted_by_the_runner_with_hashes_preserved(
    agreed: Path, tmp_path: Path
) -> None:
    pairs, dataset, inputs_path = _export(agreed, tmp_path)
    inputs = hhem.read_inputs(inputs_path)
    out = tmp_path / "predictions.jsonl"
    out.write_text(hhem.score_inputs(inputs, hhem.SyntheticBackend()), "utf-8")
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    header, rows = lines[0], lines[1:]
    exported = [json.loads(x) for x in inputs_path.read_text().splitlines()]
    assert header["inputs_sha256"] == exported[0]["inputs_sha256"] == cb.inputs_digest(pairs)
    assert [r["input_sha256"] for r in rows] == [r["input_sha256"] for r in exported[1:]]
    assert (header["model"], header["revision"]) == (hhem.HHEM_REPO, hhem.HHEM_REVISION)
    runtime = header["runtime"]
    assert runtime["reviewed_code_sha256"] == {n: hhem.HHEM_FILES[n] for n in hhem.REVIEWED_CODE}
    assert runtime["foundation"] == {"repo": "google/flan-t5-base", "revision": hhem.FLAN_REVISION}
    assert runtime["python"] and runtime["scorer_sha256"] and header["inference"] == "synthetic"
    scorer = cb.PredictionsScorer(out, pairs, allow_synthetic=True)
    report = cb.run(lambda: scorer, pairs, dataset)
    assert report["status"] == "scored" and report["metrics"]["pairs"] == 37
    assert report["scorer"]["model"] == {"model": hhem.HHEM_REPO, "revision": hhem.HHEM_REVISION}


def test_runner_refuses_synthetic_hhem_files_by_default(agreed: Path, tmp_path: Path) -> None:
    pairs, _, inputs_path = _export(agreed, tmp_path)
    out = tmp_path / "predictions.jsonl"
    out.write_text(hhem.score_inputs(hhem.read_inputs(inputs_path), hhem.SyntheticBackend()))
    with pytest.raises(cb.BackendRejected, match="synthetic test scores"):
        cb.PredictionsScorer(out, pairs)


def _rewrite(path: Path, edit: Callable[[list[dict[str, Any]]], None]) -> None:
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    edit(lines)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda ls: ls[1].update(claim=ls[1]["claim"] + " Edited."), "changed after export"),
        (lambda ls: ls[2].update(premise="A different premise."), "changed after export"),
        (lambda ls: ls[0].update(inputs_sha256="0" * 64), "inputs_sha256 does not match"),
        (lambda ls: ls.pop(), "header announces"),
        (lambda ls: ls.append(dict(ls[1])), "header announces"),
        (lambda ls: ls[0].update(kind="something-else"), "not an M7 checker-inputs export"),
    ],
)
def test_hhem_refuses_stale_or_edited_inputs(
    agreed: Path, tmp_path: Path, edit: Any, message: str
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    _rewrite(inputs_path, edit)
    with pytest.raises(hhem.InputError, match=message):
        hhem.read_inputs(inputs_path)


def test_predictions_go_stale_when_inputs_change_under_the_same_ids(
    agreed: Path, tmp_path: Path
) -> None:
    pairs, _, inputs_path = _export(agreed, tmp_path)
    out = tmp_path / "predictions.jsonl"
    out.write_text(hhem.score_inputs(hhem.read_inputs(inputs_path), hhem.SyntheticBackend()))
    changed = list(pairs)
    p0 = pairs[0]
    changed[0] = cb.EvalPair(p0.pair_id, p0.claim, p0.premise + " (revised)", p0.gold,
                             p0.change_type, p0.family)  # fmt: skip
    with pytest.raises(ValueError, match="inputs_sha256 is stale"):
        cb.PredictionsScorer(out, changed, allow_synthetic=True)


def test_hhem_scores_nothing_and_downloads_nothing_for_stale_inputs(
    agreed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    _rewrite(inputs_path, lambda ls: ls[1].update(claim="Edited."))

    def no_fetch(_: Path) -> None:
        raise AssertionError("must not download before the inputs are verified")

    monkeypatch.setattr(hhem, "fetch", no_fetch)
    with pytest.raises(hhem.InputError):
        hhem.main(["--inputs", str(inputs_path), "--out", str(tmp_path / "o.jsonl")])
    assert not (tmp_path / "o.jsonl").exists()


def _unreviewed_copy(tmp_path: Path) -> Path:
    """A local HHEM dir whose code files have the reviewed names but other bytes."""

    d = tmp_path / "hhem"
    d.mkdir()
    for name in hhem.REVIEWED_CODE:
        (d / name).write_text("# placeholder, not the reviewed code\n")
    return d


def test_unreviewed_hhem_code_is_never_loaded(tmp_path: Path) -> None:
    d = _unreviewed_copy(tmp_path)  # placeholder bytes: hashes differ from the reviewed code
    with pytest.raises(hhem.PinError, match=re.escape("configuration_hhem_v2.py: sha256")):
        hhem.verify_reviewed_code(d)
    # The backend checks the code before importing torch or transformers (neither is
    # installed here, so reaching an import would raise ImportError instead).
    with pytest.raises(hhem.PinError):
        hhem.HhemBackend(d, tmp_path / "flan", "cuda")


def test_extra_python_files_in_the_model_dir_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _unreviewed_copy(tmp_path)
    (d / "evil.py").write_text("import os\n")
    monkeypatch.setattr(hhem, "verify_file", lambda path, pinned: None)  # pretend hashes match
    with pytest.raises(hhem.PinError, match=r"unreviewed code .*evil\.py"):
        hhem.verify_reviewed_code(d)


def test_pinned_file_mismatch_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "model.safetensors"
    f.write_bytes(b"not the weights")
    with pytest.raises(hhem.PinError, match=re.escape("model.safetensors: sha256")):
        hhem.verify_file(f, hhem.HHEM_FILES["model.safetensors"])


class _FixedBackend:
    inference = "synthetic"

    def __init__(self, values: list[float]) -> None:
        self.values = values

    def describe(self) -> dict[str, Any]:
        return {}

    def score(self, premise: str, claim: str) -> float:
        return self.values.pop(0)


def test_hhem_decision_is_the_models_argmax_and_scores_are_bounded(
    agreed: Path, tmp_path: Path
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    inputs = hhem.read_inputs(inputs_path)
    values = [0.5, 0.5000001, 0.2] + [0.9] * (len(inputs.rows) - 3)
    rows = [json.loads(x) for x in hhem.score_inputs(inputs, _FixedBackend(values)).splitlines()][
        1:
    ]
    assert [r["predicted"] for r in rows[:3]] == ["unsupported", "supported", "unsupported"]
    with pytest.raises(RuntimeError, match="outside"):
        hhem.score_inputs(inputs, _FixedBackend([1.2] * len(inputs.rows)))


# ------------------------------------------- HHEM re-review: runtime gate and staging
class _Reached(Exception):
    pass


def _no_fetch(*_: Any) -> Any:
    raise AssertionError("fetch must not be called on an unsupported runtime")


def test_unsupported_runtime_exits_without_fetching(
    agreed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    monkeypatch.setattr(hhem, "fetch", _no_fetch)
    out = tmp_path / "o.jsonl"
    # This test machine itself: not Colab, no torch, no GPU.
    assert hhem.main(["--inputs", str(inputs_path), "--out", str(out)]) == 2
    assert not out.exists()


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        ({"colab": False, "torch": "2.8", "cuda": True, "device_name": "Tesla T4"}, "not a Google Colab"),
        ({"colab": True, "torch": None, "cuda": False, "device_name": None}, "torch is not installed"),
        ({"colab": True, "torch": "2.8", "cuda": False, "device_name": None}, "no CUDA device"),
        ({"colab": True, "torch": "2.8", "cuda": True, "device_name": "NVIDIA A100-SXM4-40GB"}, "not a supported target"),
        ({"colab": True, "torch": "2.8", "cuda": True, "device_name": "NVIDIA L4"}, "not a supported target"),
    ],
)  # fmt: skip
def test_each_unsupported_runtime_is_refused_before_fetch(
    agreed: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    info: dict[str, Any],
    reason: str,
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    monkeypatch.setattr(hhem, "probe_runtime", lambda: info)
    monkeypatch.setattr(hhem, "fetch", _no_fetch)
    assert hhem.main(["--inputs", str(inputs_path), "--out", str(tmp_path / "o.jsonl")]) == 2
    assert reason in capsys.readouterr().err


def test_supported_runtime_is_checked_after_inputs_and_before_fetch(
    agreed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    order: list[str] = []
    t4 = {"colab": True, "torch": "2.8", "cuda": True, "device_name": "Tesla T4"}

    def probe() -> dict[str, Any]:
        order.append("runtime")
        return t4

    monkeypatch.setattr(hhem, "probe_runtime", probe)

    def fetch(_: Path) -> Any:
        order.append("fetch")
        raise _Reached

    monkeypatch.setattr(hhem, "fetch", fetch)
    with pytest.raises(_Reached):
        hhem.main(["--inputs", str(inputs_path), "--out", str(tmp_path / "o.jsonl")])
    assert order == ["runtime", "fetch"]


def test_allow_cpu_is_gone() -> None:
    with pytest.raises(SystemExit):
        hhem.main(["--inputs", "x", "--out", "y", "--allow-cpu"])


def _pinned_tree(root: Path, files: dict[str, bytes]) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


def test_staging_copies_only_pinned_files_and_drops_extra_added_tokens(tmp_path: Path) -> None:
    src = tmp_path / "download" / "flan"
    pins = _pinned_tree(src, {"tokenizer.json": b'{"model": 1}', "spiece.model": b"spm"})
    (src / "added_tokens.json").write_text('{"Premise": 32100}')  # not pinned
    load = tmp_path / "load" / "flan"
    load.mkdir(parents=True)
    (load / "added_tokens.json").write_text('{"stale": 1}')  # left over in an old load dir
    staged = hhem.stage(src, pins, load)
    assert sorted(p.name for p in staged.iterdir()) == ["spiece.model", "tokenizer.json"]
    with pytest.raises(hhem.PinError, match=r"unexpected \['added_tokens.json'\]"):
        hhem.verify_exact_dir(src, pins)
    (src / "spiece.model").write_bytes(b"tampered")
    with pytest.raises(hhem.PinError, match=re.escape("spiece.model: sha256")):
        hhem.stage(src, pins, load)


def test_scorer_loads_only_from_the_clean_staged_dirs(
    agreed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, inputs_path = _export(agreed, tmp_path)
    code = {"configuration_hhem_v2.py": b"# c\n", "modeling_hhem_v2.py": b"# m\n"}
    hhem_pins = _pinned_tree(
        tmp_path / "src-hhem", {**code, "config.json": b"{}", "model.safetensors": b"w"}
    )
    flan_pins = _pinned_tree(tmp_path / "src-flan", {"tokenizer.json": b"{}", "spiece.model": b"s"})
    (tmp_path / "src-flan" / "added_tokens.json").write_text('{"Premise": 32100}')
    (tmp_path / "src-hhem" / "extra.py").write_text("import os\n")
    monkeypatch.setattr(hhem, "HHEM_FILES", hhem_pins)
    monkeypatch.setattr(hhem, "FLAN_FILES", flan_pins)
    t4 = {"colab": True, "torch": "2.8", "cuda": True, "device_name": "Tesla T4"}
    monkeypatch.setattr(hhem, "probe_runtime", lambda: t4)
    monkeypatch.setattr(hhem, "fetch", lambda _: (tmp_path / "src-hhem", tmp_path / "src-flan"))
    seen: dict[str, list[str]] = {}

    class Recorder(hhem.SyntheticBackend):  # type: ignore[misc, name-defined]
        def __init__(self, hhem_dir: Path, flan_dir: Path, device: str) -> None:
            hhem.verify_exact_dir(hhem_dir, hhem.HHEM_FILES)
            hhem.verify_exact_dir(flan_dir, hhem.FLAN_FILES)
            seen["hhem"] = sorted(p.name for p in hhem_dir.iterdir())
            seen["flan"] = sorted(p.name for p in flan_dir.iterdir())
            seen["device"] = [device]

    monkeypatch.setattr(hhem, "HhemBackend", Recorder)
    out = tmp_path / "o.jsonl"
    assert (
        hhem.main(
            [
                "--inputs",
                str(inputs_path),
                "--out",
                str(out),
                "--cache-dir",
                str(tmp_path / "cache"),
            ]
        )
        == 0
    )
    assert seen == {"hhem": ["config.json", "configuration_hhem_v2.py", "model.safetensors",
                             "modeling_hhem_v2.py"],
                    "flan": ["spiece.model", "tokenizer.json"], "device": ["cuda"]}  # fmt: skip
    assert out.exists()


def test_backend_refuses_load_dirs_with_unexpected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = {"configuration_hhem_v2.py": b"# c\n", "modeling_hhem_v2.py": b"# m\n"}
    hhem_pins = _pinned_tree(
        tmp_path / "hhem", {**code, "config.json": b"{}", "model.safetensors": b"w"}
    )
    flan_pins = _pinned_tree(tmp_path / "flan", {"tokenizer.json": b"{}"})
    (tmp_path / "flan" / "added_tokens.json").write_text('{"Premise": 32100}')
    monkeypatch.setattr(hhem, "HHEM_FILES", hhem_pins)
    monkeypatch.setattr(hhem, "FLAN_FILES", flan_pins)
    with pytest.raises(hhem.PinError, match=r"unexpected \['added_tokens.json'\]"):
        hhem.HhemBackend(tmp_path / "hhem", tmp_path / "flan", "cuda")  # before torch import


# ------------------------------------------------------ HHEM Colab T4 smoke test (pending)
def _load_smoke() -> Any:
    spec = importlib.util.spec_from_file_location(
        "smoke_model_card", mf.REPO / "ml" / "hhem-baseline" / "smoke_model_card.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_model_card"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()
_T4 = {"colab": True, "torch": "2.8.0", "cuda": True, "device_name": "Tesla T4"}
_PUBLISHED = [e[2] for e in smoke.EXAMPLES]


def test_smoke_uses_the_seven_published_model_card_examples() -> None:
    assert len(smoke.EXAMPLES) == 7
    assert _PUBLISHED[0] == 0.011061512865126133 and _PUBLISHED[-1] == 0.05432349815964699
    assert smoke.hhem is hhem  # the merged scorer, not a copy
    assert smoke.REQUIRED_VERSIONS["transformers"] == "4.56.2"


def test_smoke_comparison_passes_within_tolerance_and_fails_outside() -> None:
    exact = smoke.compare(_PUBLISHED)
    assert exact["passed"] and exact["max_abs_diff"] == 0.0
    near = smoke.compare([p + 0.004 for p in _PUBLISHED])
    assert near["passed"] and near["max_abs_diff"] == pytest.approx(0.004)
    off = smoke.compare([_PUBLISHED[0] + 0.02, *_PUBLISHED[1:]])
    assert not off["passed"] and not off["examples"][0]["within_tolerance"]
    flipped = list(_PUBLISHED)
    flipped[1] = 0.49  # published 0.647: decision flips (and is far outside tolerance)
    assert not smoke.compare(flipped)["examples"][1]["decision_matches"]
    with pytest.raises(ValueError):
        smoke.compare(_PUBLISHED[:6])


def test_smoke_requires_the_pinned_library_versions() -> None:
    good = dict(smoke.REQUIRED_VERSIONS)
    assert smoke.check_versions(good.get) == good
    with pytest.raises(hhem.RuntimeUnsupported, match=re.escape("4.56.2")):
        smoke.check_versions({**good, "transformers": "5.0.0"}.get)
    with pytest.raises(hhem.RuntimeUnsupported):
        smoke.check_versions({**good, "tokenizers": None}.get)


def test_smoke_latency_summary() -> None:
    s = smoke.latency_summary([10.0, 30.0, 20.0, 40.0])
    assert (s["n"], s["p50_ms"], s["p95_ms"], s["mean_ms"], s["max_ms"]) == (
        4,
        25.0,
        40.0,
        25.0,
        40.0,
    )


def test_smoke_stops_before_download_off_colab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hhem, "fetch", _no_fetch)
    code, report = smoke.run(tmp_path)  # this machine: not Colab
    assert code == 2 and report["status"] == "unsupported" and "Colab" in report["reason"]


def test_smoke_stops_before_download_on_wrong_transformers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hhem, "probe_runtime", lambda: _T4)
    monkeypatch.setattr(smoke, "installed_version", lambda name: "5.0.0")
    monkeypatch.setattr(hhem, "fetch", _no_fetch)
    code, report = smoke.run(tmp_path)
    assert code == 2 and "4.56.2" in report["reason"]


class _CardBackend:
    """Stands in for HhemBackend: returns given scores per premise (synthetic only)."""

    inference = "synthetic"
    offset = 0.0

    def __init__(self, hhem_dir: Path, flan_dir: Path, device: str) -> None:
        self.table: dict[str, float] = {p: float(s) + self.offset for p, _, s in smoke.EXAMPLES}

    def describe(self) -> dict[str, Any]:
        return {"torch": "2.8.0", "cuda": "12.6", "device": "cuda", "device_name": "Tesla T4"}

    def score(self, premise: str, claim: str) -> float:
        return self.table[premise]


@pytest.mark.parametrize(("offset", "code", "status"), [(0.0, 0, "passed"), (0.05, 1, "failed")])
def test_smoke_run_records_versions_latency_and_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: float, code: int, status: str
) -> None:
    monkeypatch.setattr(hhem, "probe_runtime", lambda: _T4)
    monkeypatch.setattr(smoke, "installed_version", smoke.REQUIRED_VERSIONS.get)
    monkeypatch.setattr(hhem, "fetch", lambda d: (tmp_path / "h", tmp_path / "f"))
    monkeypatch.setattr(hhem, "stage_all", lambda d, h, f: (h, f))
    backend = type("B", (_CardBackend,), {"offset": offset})
    monkeypatch.setattr(hhem, "HhemBackend", backend)
    got_code, report = smoke.run(tmp_path)
    assert (got_code, report["status"]) == (code, status)
    assert report["runtime"]["device_name"] == "Tesla T4"
    assert report["versions"]["transformers"] == "4.56.2" and report["load_seconds"] >= 0
    assert report["latency_per_example"]["n"] == 7 * smoke.REPEATS
    assert len(report["first_pass_latency_ms"]) == 7
    assert (report["revision"], report["model_card_sha256"]) == (
        hhem.HHEM_REVISION, smoke.MODEL_CARD_SHA256
    )  # fmt: skip


def test_t4_notebook_is_clean_and_runs_the_pinned_smoke_test() -> None:
    nb = json.loads((mf.REPO / "ml" / "hhem-baseline" / "hhem_smoke_t4.ipynb").read_text())
    assert nb["nbformat"] == 4 and nb["metadata"]["colab"]["gpuType"] == "T4"
    text = "".join("".join(c["source"]) for c in nb["cells"])
    assert "PENDING" in text and "T4 GPU" in text
    assert "pip install -q -r requirements.txt" in text
    assert "python smoke_model_card.py --out /content/hhem-smoke-report.json" in text
    assert "https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git" in text
    code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
    assert all(c["outputs"] == [] and c["execution_count"] is None for c in code_cells)
