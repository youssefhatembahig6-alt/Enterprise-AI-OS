"""Two-annotator labelling for the gold checker pairs: blind sheets, Cohen's kappa,
disagreement tracking and adjudication.

Files (backend/fixtures/evaluation/m7/pilot/labels/):
  gold-<annotator>.csv   one annotator's labels: pair_id,label[,note] (other columns ignored)
  adjudications.csv      pair_id,final_label,adjudicator,reason — one row per disagreement

A pair is GOLD only when two different annotators agree, or when they disagree and an
adjudication row resolves it. The AI author's ``proposed_label`` is never a human label
and is never shown on a sheet.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

from app.evaluation.pilot import GENERATED, PILOT
from app.evaluation.sources import BACKEND

LABELS = ("supported", "unsupported")
LABEL_DIR = PILOT / "labels"
ADJUDICATIONS = "adjudications.csv"
SHEET_COLUMNS = ("pair_id", "claim", "evidence_title", "evidence", "label", "note")


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa for two raters over the same items; ``None`` when undefined."""

    if len(a) != len(b):
        raise ValueError("raters must label the same items")
    n = len(a)
    if n == 0:
        return None
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if expected == 1.0:
        return None  # both raters used one identical category: agreement by chance only
    return (observed - expected) / (1 - expected)


def gold_pairs() -> list[dict[str, Any]]:
    path = GENERATED / "gold-checker-pairs.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def export_sheet(annotator: str, out: Path | None = None) -> Path:
    """A blind sheet: no proposed label, no other annotator's labels, own item order."""

    if not annotator.replace("-", "").replace("_", "").isalnum():
        raise ValueError("annotator id: letters, digits, '-' and '_' only")
    pairs = sorted(
        gold_pairs(),
        key=lambda p: hashlib.sha256(f"{annotator}|{p['id']}".encode()).hexdigest(),
    )
    out = out or BACKEND.parent / "var" / "m7-labels" / f"gold-{annotator}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(SHEET_COLUMNS)
        for p in pairs:
            writer.writerow([p["id"], p["claim"], p["evidence_title"], p["evidence"], "", ""])
    return out


def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text("utf-8-sig"))))


def read_labels(label_dir: Path = LABEL_DIR) -> tuple[dict[str, dict[str, str]], list[str]]:
    """annotator -> {pair_id: label}; plus problems."""

    problems: list[str] = []
    out: dict[str, dict[str, str]] = {}
    known = {p["id"] for p in gold_pairs()}
    for path in sorted(label_dir.glob("gold-*.csv")):
        annotator = path.stem.removeprefix("gold-")
        labels: dict[str, str] = {}
        for row in _read_csv(path):
            pid, label = row.get("pair_id", "").strip(), row.get("label", "").strip().lower()
            if not label:
                continue
            if pid not in known:
                problems.append(f"{path.name}: unknown pair {pid!r}")
            elif label not in LABELS:
                problems.append(f"{path.name}: {pid} has invalid label {label!r}")
            elif pid in labels:
                problems.append(f"{path.name}: {pid} labelled twice")
            else:
                labels[pid] = label
        out[annotator] = labels
    if len(out) > 2:
        problems.append(f"pilot supports exactly two annotators, found {sorted(out)}")
    return out, problems


def status(label_dir: Path = LABEL_DIR) -> dict[str, Any]:
    pairs = gold_pairs()
    by_annotator, problems = read_labels(label_dir)
    names = sorted(by_annotator)[:2]
    adjudications: dict[str, dict[str, str]] = {}
    adj_path = label_dir / ADJUDICATIONS
    if adj_path.exists():
        for row in _read_csv(adj_path):
            pid = row.get("pair_id", "").strip()
            if not pid:
                continue
            if row.get("final_label", "").strip().lower() not in LABELS:
                problems.append(f"{ADJUDICATIONS}: {pid} has invalid final_label")
            if not row.get("adjudicator", "").strip():
                problems.append(f"{ADJUDICATIONS}: {pid} has no adjudicator")
            adjudications[pid] = row
    states: dict[str, str] = {}
    a_labels: list[str] = []
    b_labels: list[str] = []
    disagreements = []
    gold: dict[str, str] = {}
    for p in pairs:
        pid = p["id"]
        got = [by_annotator[n][pid] for n in names if pid in by_annotator[n]]
        if len(got) == 0:
            states[pid] = "unlabelled"
        elif len(got) == 1:
            states[pid] = "single-label"
        elif got[0] == got[1]:
            states[pid] = "agreed"
            gold[pid] = got[0]
        elif pid in adjudications:
            states[pid] = "adjudicated"
            gold[pid] = adjudications[pid]["final_label"].strip().lower()
        else:
            states[pid] = "disagreement-pending"
        if len(got) == 2:
            a_labels.append(got[0])
            b_labels.append(got[1])
            if got[0] != got[1]:
                disagreements.append(
                    {
                        "pair_id": pid,
                        names[0]: got[0],
                        names[1]: got[1],
                        "adjudicated": pid in adjudications,
                    }
                )
    for pid in adjudications:
        if states.get(pid) not in {"adjudicated"}:
            problems.append(f"{ADJUDICATIONS}: {pid} is not a recorded disagreement")
    kappa = cohen_kappa(a_labels, b_labels)
    return {
        "pairs": len(pairs),
        "annotators": names,
        "labels_per_annotator": {n: len(by_annotator[n]) for n in names},
        "double_labelled": len(a_labels),
        "agreed": sum(1 for s in states.values() if s == "agreed"),
        "disagreements": disagreements,
        "adjudicated": sum(1 for s in states.values() if s == "adjudicated"),
        "cohen_kappa": None if kappa is None else round(kappa, 4),
        "state_counts": dict(sorted(Counter(states.values()).items())),
        "human_reviewed_pair_ids": sorted(pid for pid, s in states.items() if s != "unlabelled"),
        "gold_labels": len(gold),
        "problems": problems,
    }
