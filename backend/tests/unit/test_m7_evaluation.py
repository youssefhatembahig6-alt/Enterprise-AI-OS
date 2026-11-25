"""M7 evaluation foundation: frozen splits, freeze enforcement, provenance, pilot, labels.

Every failure the M7 smoke check claims to catch is reproduced here on a mutated copy, so
a check that passes everything would fail these tests. The PR #10 review findings each
have their own section.
"""

from __future__ import annotations

import copy
import csv
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


def test_pilot_counts_are_all_dev_under_v2() -> None:
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
def test_v2_assigns_every_source_once_and_passes_the_exposure_audit() -> None:
    manifest = _parse(V2_TEXT)
    assert set(manifest.family_split.values()) == {"dev"}
    assert set(manifest.reserved.values()) == {"train", "heldout"}
    assert mf.audit_exposure(manifest) == []


def test_v1_is_kept_and_its_misassignments_are_caught_by_the_audit() -> None:
    problems = mf.audit_exposure(_parse(V1_TEXT))
    flagged = {p.split()[1] for p in problems if "development artifacts mention it" in p}
    assert flagged == {"code-config-constants", "contract-template", "board-governance"}


def test_moving_an_exposed_family_out_of_dev_fails_the_audit() -> None:
    text = V2_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"')
    problems = mf.audit_exposure(_parse(text))
    assert any("board-governance is heldout but development artifacts" in p for p in problems)


def test_listed_exposure_must_exist_and_mention_a_member() -> None:
    text = V2_TEXT.replace(
        'exposure = ["backend/tests/integration/test_m5_legal.py"]',
        'exposure = ["backend/tests/unit/test_policy_evaluator.py", "docs/nope.md"]',
    )
    problems = mf.audit_exposure(_parse(text))
    assert any("test_policy_evaluator.py mentions no member" in p for p in problems)
    assert any("docs/nope.md does not exist" in p for p in problems)


def test_unassigned_source_and_double_assignment_fail() -> None:
    _, problems = mf.parse_manifest(
        V2_TEXT.replace('members = ["doc:customers-csv"]', "members = []")
    )
    assert any("doc:customers-csv is not assigned" in p for p in problems)
    _, problems = mf.parse_manifest(
        V2_TEXT.replace(
            'members = ["doc:customers-csv"]',
            'members = ["doc:customers-csv", "doc:hr-leave-policy"]',
        )
    )
    assert any("doc:hr-leave-policy is in two families" in p for p in problems)


def test_reserved_families_cannot_hold_members() -> None:
    text = V2_TEXT.replace(
        'id = "fresh-train-regenerated"\nsplit = "train"',
        'id = "fresh-train-regenerated"\nsplit = "train"\nmembers = ["doc:x"]',
    )
    _, problems = mf.parse_manifest(text)
    assert any("must not have members" in p for p in problems)


def test_moving_a_dev_exposed_family_is_caught_via_legacy_fixtures() -> None:
    text = V2_TEXT.replace(
        'id = "hr-leave-policy"\nsplit = "dev"', 'id = "hr-leave-policy"\nsplit = "heldout"'
    )
    _, problems = legacy_assignments(_parse(text))
    assert any("hr-leave-policy is heldout" in p and "cross splits" in p for p in problems)


# --------------------------------------------------- finding 4: enforceable freeze
def _tampered_overlay(text: str) -> dict[str, str]:
    """Change the manifest, then regenerate the lock AND the pilot to match it."""

    manifest = _parse(text)
    overlay = {
        "split-manifest-v2.toml": text,
        "split-manifest-v2.lock.json": mf.lock_text(
            mf.build_lock(manifest, "split-manifest-v2.toml")
        ),
    }
    overlay.update({f"pilot/{rel}": body for rel, body in render(manifest).items()})
    return overlay


def test_changed_manifest_with_regenerated_lock_and_pilot_still_fails() -> None:
    text = V2_TEXT.replace(
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
    manifest = _parse(V2_TEXT)
    lock = mf.load_lock(M7 / "split-manifest-v2.lock.json")
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
        V2_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"')
    )
    (m7 / "split-manifest-v2.toml").write_text(overlay["split-manifest-v2.toml"])
    (m7 / "split-manifest-v2.lock.json").write_text(overlay["split-manifest-v2.lock.json"])
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    entry = snaps["manifests"]["m7-split-v2"]
    entry["manifest_sha256"] = freeze._sha(overlay["split-manifest-v2.toml"])
    entry["lock_sha256"] = freeze._sha(overlay["split-manifest-v2.lock.json"])
    (m7 / "frozen-snapshots.json").write_text(json.dumps(snaps))
    _git(repo, "commit", "-q", "-am", "rewrite the freeze")
    problems = freeze.check_against_base("main", repo, freeze.M7_REL)
    assert any("split-manifest-v2.toml is frozen on main" in p for p in problems)
    assert any("split-manifest-v2.lock.json is frozen on main" in p for p in problems)
    assert any("m7-split-v2.manifest_sha256 on main was changed" in p for p in problems)


def test_base_allows_only_active_to_superseded(frozen_repo: tuple[Path, Path]) -> None:
    repo, m7 = frozen_repo
    snaps = json.loads((m7 / "frozen-snapshots.json").read_text())
    snaps["manifests"]["m7-split-v2"].update(status="superseded", superseded_by="v3", reason="r")
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

    return _parse(V2_TEXT.replace(CODE_DEV, 'id = "code-config-constants"\nsplit = "train"'))


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
    manifest = _parse(V2_TEXT.replace(BOARD_DEV, 'id = "board-governance"\nsplit = "heldout"'))
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
