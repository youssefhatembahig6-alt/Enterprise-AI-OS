"""M7 evaluation foundation: frozen splits, freeze enforcement, provenance, pilot, labels.

Every failure the M7 smoke check claims to catch is reproduced here on a mutated copy, so
a check that passes everything would fail these tests. The PR #10 review findings each
have their own section.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import re
import shutil
import subprocess
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
