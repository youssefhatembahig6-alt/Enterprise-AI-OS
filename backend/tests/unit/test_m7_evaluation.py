"""M7 evaluation foundation: the frozen split, leakage checks, pilot datasets, labels.

Every failure mode the M7 smoke check claims to catch is exercised here on a mutated
copy, so a check that silently passes everything would fail these tests.
"""

from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest

from app.evaluation import labels as lab
from app.evaluation.checks import check_records, legacy_assignments, run_checks
from app.evaluation.manifest import MANIFEST, load_lock, parse_manifest, verify_lock
from app.evaluation.pilot import build_records, render

MANIFEST_TEXT = MANIFEST.read_text("utf-8")


def _manifest(text: str = MANIFEST_TEXT):  # type: ignore[no-untyped-def]
    manifest, problems = parse_manifest(text)
    return manifest, problems


# ------------------------------------------------------------------ repository state
def test_committed_state_passes_the_m7_check() -> None:
    problems, summary = run_checks()
    assert problems == []
    assert "PILOT datasets" in summary and "Cohen's kappa" in summary


def test_pilot_build_is_byte_identical_across_runs() -> None:
    assert render() == render()


def test_pilot_counts_and_splits() -> None:
    data = build_records()
    assert {k: len(v) for k, v in data.items()} == {
        "qa-cases": 12,
        "retrieval-pairs": 57,
        "checker-train-pairs": 78,
        "gold-checker-pairs": 37,
    }
    assert {r["split"] for r in data["checker-train-pairs"]} == {"train"}
    assert {r["split"] for r in data["gold-checker-pairs"]} == {"dev"}
    assert {r["split"] for r in data["qa-cases"]} == {"dev"}
    every = [r for recs in data.values() for r in recs]
    assert all(r["pilot"] is True and r["release"] is False for r in every)
    assert not any(r["split"] == "heldout" for r in every)


def test_gold_pairs_never_carry_a_label_field_only_a_proposal() -> None:
    for rec in build_records()["gold-checker-pairs"]:
        assert "label" not in rec and rec["proposed_label"] in lab.LABELS


# ------------------------------------------------------------------------ manifest
def test_manifest_assigns_every_source_exactly_once() -> None:
    _, problems = _manifest()
    assert problems == []


def test_unassigned_source_fails() -> None:
    text = MANIFEST_TEXT.replace('members = ["doc:customers-csv"]', "members = []")
    _, problems = _manifest(text)
    assert any("doc:customers-csv is not assigned" in p for p in problems)


def test_source_in_two_families_fails() -> None:
    text = MANIFEST_TEXT.replace(
        'members = ["doc:customers-csv"]', 'members = ["doc:customers-csv", "doc:hr-leave-policy"]'
    )
    _, problems = _manifest(text)
    assert any("doc:hr-leave-policy is in two families" in p for p in problems)


def test_editing_the_frozen_manifest_fails_the_lock() -> None:
    manifest, _ = _manifest(MANIFEST_TEXT + "\n# harmless-looking edit\n")
    assert any("changed after it was frozen" in p for p in verify_lock(manifest, load_lock()))


def test_changed_member_source_fails_the_lock() -> None:
    manifest, _ = _manifest()
    lock = load_lock()
    lock["member_sha256"]["doc:hr-leave-policy"] = "0" * 64
    assert any("doc:hr-leave-policy changed" in p for p in verify_lock(manifest, lock))


def test_moving_a_dev_exposed_family_to_heldout_is_caught_via_legacy_fixtures() -> None:
    text = MANIFEST_TEXT.replace(
        'id = "hr-leave-policy"\nsplit = "dev"', 'id = "hr-leave-policy"\nsplit = "heldout"'
    )
    manifest, problems = _manifest(text)
    assert problems == []
    _, legacy_problems = legacy_assignments(manifest)
    crossing = [p for p in legacy_problems if "hr-leave-policy is heldout" in p]
    assert crossing and all("cross splits" in p for p in crossing)


# ------------------------------------------------------------------------- records
@pytest.fixture
def data():  # type: ignore[no-untyped-def]
    return copy.deepcopy(build_records())


def _problems(data) -> list[str]:  # type: ignore[no-untyped-def]
    manifest, _ = _manifest()
    return check_records(manifest, data)


def test_untouched_records_pass(data) -> None:  # type: ignore[no-untyped-def]
    assert _problems(data) == []


def test_record_in_the_wrong_split_fails(data) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["split"] = "train"
    assert any("crosses splits" in p for p in _problems(data))


def test_derived_example_in_another_family_fails(data) -> None:  # type: ignore[no-untyped-def]
    pairs = data["checker-train-pairs"]
    contract = next(r for r in pairs if r["family"] == "contract-template")
    code = next(r for r in pairs if r["family"] == "code-config-constants")
    code["derived_from"] = contract["derived_from"]
    assert any("derived example crosses families" in p for p in _problems(data))


def test_derived_example_in_another_split_fails(data) -> None:  # type: ignore[no-untyped-def]
    train = data["checker-train-pairs"][0]
    leaked = copy.deepcopy(data["gold-checker-pairs"][0])
    leaked["id"] = "leaked-copy"
    leaked["derived_from"] = train["derived_from"]
    data["gold-checker-pairs"].append(leaked)
    assert any("derived example crosses families" in p for p in _problems(data))


def test_mixed_family_evidence_fails(data) -> None:  # type: ignore[no-untyped-def]
    data["checker-train-pairs"][0]["sources"].append("doc:hr-leave-policy")
    assert any("several families" in p for p in _problems(data))


def test_sealed_heldout_family_fails(data) -> None:  # type: ignore[no-untyped-def]
    rec = data["retrieval-pairs"][0]
    rec.update(
        sources=["doc:board-minutes"],
        source="doc:board-minutes",
        family="board-governance",
        split="heldout",
        quote="Riyadh office",
    )
    assert any("sealed held-out family board-governance" in p for p in _problems(data))


def test_same_text_in_two_splits_fails(data) -> None:  # type: ignore[no-untyped-def]
    dev_claim = data["gold-checker-pairs"][0]["claim"]
    data["checker-train-pairs"][0]["claim"] = dev_claim.replace("21", "22")
    problems = _problems(data)
    assert any("text appears in splits ['dev', 'train']" in p for p in problems)


def test_evidence_must_be_an_exact_span(data) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["evidence"] += " (paraphrased)"
    assert any("not an exact span" in p for p in _problems(data))


def test_sql_claim_must_state_its_fixture_value(data) -> None:  # type: ignore[no-untyped-def]
    qa = next(r for r in data["qa-cases"] if r["id"] == "qa-pilot-001")
    qa["claims"][0]["text"] = "NileTech's net fulfilled sales in Q1 2026 were USD 80.9 million."
    assert any("does not state fixture value 80934385.00" in p for p in _problems(data))


@pytest.mark.parametrize(
    "claim",
    [
        "Egypt gets 21 days; the UAE gets 30 days.",
        "Egypt gets 21 days. The UAE gets 30 days.",
        " ".join(["word"] * 31) + ".",
    ],
)
def test_non_atomic_claims_fail(data, claim: str) -> None:  # type: ignore[no-untyped-def]
    data["gold-checker-pairs"][0]["claim"] = claim
    assert any("claim" in p and data["gold-checker-pairs"][0]["id"] in p for p in _problems(data))


# -------------------------------------------------------------------------- labels
def test_cohen_kappa_known_value() -> None:
    # 50 items: 20 yes/yes, 15 no/no, 5 yes/no, 10 no/yes -> po 0.7, pe 0.5, kappa 0.4
    a = ["s"] * 20 + ["u"] * 15 + ["s"] * 5 + ["u"] * 10
    b = ["s"] * 20 + ["u"] * 15 + ["u"] * 5 + ["s"] * 10
    assert lab.cohen_kappa(a, b) == pytest.approx(0.4)


def test_cohen_kappa_edge_cases() -> None:
    assert lab.cohen_kappa([], []) is None
    assert lab.cohen_kappa(["s", "s"], ["s", "s"]) is None  # chance agreement only
    assert lab.cohen_kappa(["s", "u"], ["s", "u"]) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        lab.cohen_kappa(["s"], [])


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_two_annotator_workflow_tracks_disagreement_and_adjudication(tmp_path: Path) -> None:
    ids = [p["id"] for p in lab.gold_pairs()][:4]
    _write(
        tmp_path / "gold-alice.csv",
        [
            {"pair_id": ids[0], "label": "supported"},
            {"pair_id": ids[1], "label": "unsupported"},
            {"pair_id": ids[2], "label": "supported"},
            {"pair_id": ids[3], "label": "supported"},
        ],
    )
    _write(
        tmp_path / "gold-bob.csv",
        [
            {"pair_id": ids[0], "label": "supported"},
            {"pair_id": ids[1], "label": "unsupported"},
            {"pair_id": ids[2], "label": "unsupported"},
        ],
    )
    before = lab.status(tmp_path)
    assert before["double_labelled"] == 3 and before["agreed"] == 2
    assert [d["pair_id"] for d in before["disagreements"]] == [ids[2]]
    assert before["state_counts"]["disagreement-pending"] == 1
    assert before["state_counts"]["single-label"] == 1
    assert before["gold_labels"] == 2
    # alice s,u,s / bob s,u,u: po=2/3, pe=(2*1+1*2)/9=4/9, kappa=(2/3-4/9)/(5/9)=0.4
    assert before["cohen_kappa"] == pytest.approx(0.4)
    assert set(before["human_reviewed_pair_ids"]) == set(ids)

    _write(
        tmp_path / lab.ADJUDICATIONS,
        [
            {
                "pair_id": ids[2],
                "final_label": "unsupported",
                "adjudicator": "carol",
                "reason": "evidence omits the qualifier",
            },
        ],
    )
    after = lab.status(tmp_path)
    assert after["adjudicated"] == 1 and after["gold_labels"] == 3 and after["problems"] == []


def test_label_files_are_validated(tmp_path: Path) -> None:
    ids = [p["id"] for p in lab.gold_pairs()][:2]
    _write(
        tmp_path / "gold-alice.csv",
        [
            {"pair_id": ids[0], "label": "maybe"},
            {"pair_id": "no-such-pair", "label": "supported"},
        ],
    )
    _write(
        tmp_path / lab.ADJUDICATIONS,
        [
            {"pair_id": ids[1], "final_label": "supported", "adjudicator": "", "reason": ""},
        ],
    )
    problems = lab.status(tmp_path)["problems"]
    assert any("invalid label 'maybe'" in p for p in problems)
    assert any("unknown pair 'no-such-pair'" in p for p in problems)
    assert any("no adjudicator" in p for p in problems)
    assert any("not a recorded disagreement" in p for p in problems)


def test_blind_sheet_hides_proposals_and_orders_per_annotator(tmp_path: Path) -> None:
    a = lab.export_sheet("alice", tmp_path / "a.csv")
    b = lab.export_sheet("bob", tmp_path / "b.csv")
    rows_a = list(csv.DictReader(a.open(encoding="utf-8")))
    rows_b = list(csv.DictReader(b.open(encoding="utf-8")))
    assert tuple(rows_a[0]) == lab.SHEET_COLUMNS
    assert "proposed_label" not in rows_a[0] and all(r["label"] == "" for r in rows_a)
    assert [r["pair_id"] for r in rows_a] != [r["pair_id"] for r in rows_b]
    assert sorted(r["pair_id"] for r in rows_a) == sorted(r["pair_id"] for r in rows_b)
