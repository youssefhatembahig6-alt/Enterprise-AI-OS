"""Two-annotator labelling for the gold checker pairs: blind sheets with opaque ids,
import of both submissions together, Cohen's kappa, disagreements and adjudication.

Blinding. A sheet shows only an opaque random ``item_id``, the claim, the evidence title
and the evidence. It never shows the pair id (which names the perturbation type), the
proposed label or the other annotator's labels, and each sheet has its own ids and its
own order. The id -> pair mapping is stored only in a key file that the coordinator
keeps (``var/m7-labels/``, git-ignored). Annotators never need repository access, and
must not open ``pilot/generated/``, which holds the proposals.

Privacy until both finish. ``import_sheets`` accepts exactly two sheets from two different
annotators in one call, and refuses if either annotator's labels are already in the
repository. No single submission can land in the repository, or be seen by the other
annotator, before both are complete.

Files (backend/fixtures/evaluation/m7/pilot/labels/):
  gold-<annotator>.csv   pair_id,label,note,dataset_revision. Written only by import.
  adjudications.csv      pair_id,final_label,adjudicator,reason. One row per disagreement.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.evaluation.pilot import GENERATED, PILOT
from app.evaluation.sources import BACKEND

LABELS = ("supported", "unsupported")
LABEL_DIR = PILOT / "labels"
SHEET_DIR = BACKEND.parent / "var" / "m7-labels"
ADJUDICATIONS = "adjudications.csv"
SHEET_COLUMNS = (
    "item_id",
    "claim",
    "evidence_title",
    "evidence",
    "label",
    "note",
    "annotator",
    "dataset_revision",
)
LABEL_COLUMNS = ("pair_id", "label", "note", "dataset_revision")
_ANNOTATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


class LabelError(ValueError):
    """A sheet, key or label file that must not be imported."""


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


def _gold_text() -> str:
    return (GENERATED / "gold-checker-pairs.jsonl").read_text("utf-8")


def dataset_revision() -> str:
    """SHA-256 of the gold pair file: a sheet is valid only for this exact revision."""

    return hashlib.sha256(_gold_text().encode("utf-8")).hexdigest()


def gold_pairs() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _gold_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------- export
def _token() -> str:
    return secrets.token_hex(6)


def export_sheet(
    annotator: str,
    out_dir: Path | None = None,
    token: Callable[[], str] = _token,
) -> tuple[Path, Path]:
    """Write a blind sheet for the annotator and a private key for the coordinator."""

    if not _ANNOTATOR.match(annotator):
        raise LabelError("annotator id: 1-40 letters, digits, '-' or '_'")
    out_dir = out_dir or SHEET_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    revision = dataset_revision()
    mapping: dict[str, str] = {}
    rows = []
    for pair in gold_pairs():
        item = f"item-{token()}"
        while item in mapping:
            item = f"item-{token()}"
        mapping[item] = pair["id"]
        rows.append((item, pair))
    rows.sort(key=lambda r: r[0])  # random ids, so a random order per sheet
    sheet = out_dir / f"gold-{annotator}.sheet.csv"
    key = out_dir / f"gold-{annotator}.key.json"
    with sheet.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(SHEET_COLUMNS)
        for item, p in rows:
            writer.writerow(
                [item, p["claim"], p["evidence_title"], p["evidence"], "", "", annotator, revision]
            )
    key.write_text(
        json.dumps(
            {"annotator": annotator, "dataset_revision": revision, "items": mapping},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "utf-8",
    )
    return sheet, key


# --------------------------------------------------------------------------- import
def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text("utf-8-sig"))))


def _decode(sheet: Path, key: Path, revision: str) -> tuple[str, list[dict[str, str]]]:
    keydata = json.loads(key.read_text("utf-8"))
    annotator: str = keydata["annotator"]
    mapping: dict[str, str] = keydata["items"]
    if keydata["dataset_revision"] != revision:
        raise LabelError(
            f"{key.name} was issued for dataset revision {keydata['dataset_revision'][:12]}…, "
            f"but the current gold pairs are revision {revision[:12]}…; re-export the sheets"
        )
    rows = _read_csv(sheet)
    if not rows or set(SHEET_COLUMNS) - set(rows[0]):
        raise LabelError(f"{sheet.name}: not a label sheet (columns {SHEET_COLUMNS})")
    seen: set[str] = set()
    out = []
    for row in rows:
        item = row["item_id"].strip()
        if row["dataset_revision"].strip() != revision:
            raise LabelError(
                f"{sheet.name}: row {item} is from dataset revision "
                f"{row['dataset_revision'][:12]}…, current is {revision[:12]}…"
            )
        if row["annotator"].strip() != annotator:
            raise LabelError(f"{sheet.name}: row {item} belongs to {row['annotator']!r}")
        if item not in mapping:
            raise LabelError(f"{sheet.name}: unknown item {item!r} (wrong key or edited id)")
        if item in seen:
            raise LabelError(f"{sheet.name}: item {item} appears twice")
        seen.add(item)
        label = row["label"].strip().lower()
        if label and label not in LABELS:
            raise LabelError(f"{sheet.name}: item {item} has invalid label {label!r}")
        out.append(
            {
                "pair_id": mapping[item],
                "label": label,
                "note": row["note"].strip(),
                "dataset_revision": revision,
            }
        )
    missing = set(mapping) - seen
    if missing:
        raise LabelError(f"{sheet.name}: {len(missing)} item(s) were deleted from the sheet")
    out.sort(key=lambda r: r["pair_id"])
    return annotator, out


def import_sheets(
    submissions: list[tuple[Path, Path]], label_dir: Path = LABEL_DIR
) -> dict[str, Path]:
    """Validate both (sheet, key) submissions, then write both label files, or neither."""

    if len(submissions) != 2:
        raise LabelError(
            "import exactly two submissions together, so neither annotator's labels are "
            "visible before both have finished"
        )
    revision = dataset_revision()
    decoded = [_decode(sheet, key, revision) for sheet, key in submissions]
    names = [a for a, _ in decoded]
    if names[0] == names[1]:
        raise LabelError("the two submissions must come from different annotators")
    for name in names:
        if (label_dir / f"gold-{name}.csv").exists():
            raise LabelError(f"labels for {name} are already imported; never overwrite them")
    written: dict[str, Path] = {}
    label_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in decoded:
        path = label_dir / f"gold-{name}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        written[name] = path
    return written


# --------------------------------------------------------------------------- status
def read_labels(label_dir: Path = LABEL_DIR) -> tuple[dict[str, dict[str, str]], list[str]]:
    """annotator -> {pair_id: label}; plus problems."""

    problems: list[str] = []
    out: dict[str, dict[str, str]] = {}
    known = {p["id"] for p in gold_pairs()}
    revision = dataset_revision()
    for path in sorted(label_dir.glob("gold-*.csv")):
        annotator = path.stem.removeprefix("gold-")
        labels: dict[str, str] = {}
        for row in _read_csv(path):
            pid, label = row.get("pair_id", "").strip(), row.get("label", "").strip().lower()
            if row.get("dataset_revision", "").strip() != revision:
                problems.append(f"{path.name}: {pid} was labelled on another dataset revision")
                continue
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
    if len(out) == 1:
        problems.append(
            "only one annotator's labels are imported; import both submissions together"
        )
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
        if states.get(pid) != "adjudicated":
            problems.append(f"{ADJUDICATIONS}: {pid} is not a recorded disagreement")
    kappa = cohen_kappa(a_labels, b_labels)
    return {
        "pairs": len(pairs),
        "dataset_revision": dataset_revision(),
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
