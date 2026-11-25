"""Checker metrics (pure, deterministic). "unsupported" is the positive class.

Definitions (Member 5 report, section 4.3; roadmap "M7 evaluation details"):

* precision / recall / F1 for **unsupported**: TP = gold unsupported and predicted
  unsupported; FP = gold supported, predicted unsupported; FN = gold unsupported, predicted
  supported.
* **false-support rate (headline)**: of the gold-unsupported claims, the share predicted
  supported, i.e. how often a wrong claim passes: FN / (TP + FN) = 1 - recall. Reported with
  a Wilson 95% interval, because dev sets are small.
* **calibration** of P(supported): expected calibration error (ECE) over 10 equal-width
  bins, the maximum bin error (MCE), and the Brier score. A scorer that outputs only hard
  0/1 labels (an LLM judge) is reported, but marked ``hard_labels``.
* **accuracy per change type** (original, number, entity, scope, ...).
* **latency per claim**: nearest-rank p50 / p95, mean and max, in milliseconds.

Undefined values (a zero denominator) are ``None``, never 0, so an empty class cannot look
like a perfect score.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Scored:
    pair_id: str
    gold: str  # "supported" | "unsupported" (human-reviewed)
    p_supported: float  # the scorer's probability that the claim is supported
    predicted: str  # the scorer's decision at its stated operating point
    latency_ms: float
    change_type: str


def _ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def confusion(rows: Sequence[Scored]) -> dict[str, int]:
    tp = sum(r.gold == UNSUPPORTED and r.predicted == UNSUPPORTED for r in rows)
    fp = sum(r.gold == SUPPORTED and r.predicted == UNSUPPORTED for r in rows)
    fn = sum(r.gold == UNSUPPORTED and r.predicted == SUPPORTED for r in rows)
    tn = sum(r.gold == SUPPORTED and r.predicted == SUPPORTED for r in rows)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def unsupported_prf(rows: Sequence[Scored]) -> dict[str, float | None]:
    c = confusion(rows)
    precision = _ratio(c["tp"], c["tp"] + c["fp"])
    recall = _ratio(c["tp"], c["tp"] + c["fn"])
    f1 = (
        None
        if precision is None or recall is None
        else (0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    )
    return {"precision": precision, "recall": recall, "f1": f1}


def wilson(successes: int, n: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    """Wilson score interval for a proportion (95% by default)."""

    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def false_support_rate(rows: Sequence[Scored]) -> dict[str, Any]:
    c = confusion(rows)
    wrong = c["tp"] + c["fn"]
    ci = wilson(c["fn"], wrong)
    return {
        "rate": _ratio(c["fn"], wrong),
        "passed_wrong_claims": c["fn"],
        "gold_unsupported": wrong,
        "wilson95": None if ci is None else [ci[0], ci[1]],
    }


def calibration(rows: Sequence[Scored], bins: int = 10) -> dict[str, Any]:
    if not rows:
        return {"ece": None, "mce": None, "brier": None, "bins": [], "hard_labels": None}
    table: list[dict[str, Any]] = []
    ece = 0.0
    mce = 0.0
    n = len(rows)
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [
            r for r in rows if lo <= r.p_supported < hi or (b == bins - 1 and r.p_supported == 1.0)
        ]
        if not members:
            continue
        confidence = sum(r.p_supported for r in members) / len(members)
        observed = sum(r.gold == SUPPORTED for r in members) / len(members)
        gap = abs(confidence - observed)
        ece += len(members) / n * gap
        mce = max(mce, gap)
        table.append(
            {
                "range": [lo, hi],
                "n": len(members),
                "mean_p_supported": confidence,
                "observed_supported": observed,
            }
        )
    brier = sum((r.p_supported - (r.gold == SUPPORTED)) ** 2 for r in rows) / n
    hard = all(r.p_supported in (0.0, 1.0) for r in rows)
    return {"ece": ece, "mce": mce, "brier": brier, "bins": table, "hard_labels": hard}


def accuracy_by_change_type(rows: Sequence[Scored]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Scored]] = defaultdict(list)
    for r in rows:
        groups[r.change_type].append(r)
    return {
        kind: {
            "n": len(members),
            "accuracy": sum(r.gold == r.predicted for r in members) / len(members),
        }
        for kind, members in sorted(groups.items())
    }


def nearest_rank(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def latency(rows: Sequence[Scored]) -> dict[str, float | None]:
    values = [r.latency_ms for r in rows]
    return {
        "p50_ms": nearest_rank(values, 0.50),
        "p95_ms": nearest_rank(values, 0.95),
        "mean_ms": None if not values else sum(values) / len(values),
        "max_ms": None if not values else max(values),
    }


def summarize(rows: Sequence[Scored]) -> dict[str, Any]:
    labels = [r.gold for r in rows]
    if any(label not in (SUPPORTED, UNSUPPORTED) for label in labels):
        raise ValueError("gold labels must be supported/unsupported")
    if any(r.predicted not in (SUPPORTED, UNSUPPORTED) for r in rows):
        raise ValueError("predictions must be supported/unsupported")
    if any(not 0.0 <= r.p_supported <= 1.0 for r in rows):
        raise ValueError("p_supported must be within [0, 1]")
    return {
        "pairs": len(rows),
        "gold": {SUPPORTED: labels.count(SUPPORTED), UNSUPPORTED: labels.count(UNSUPPORTED)},
        "confusion_unsupported_positive": confusion(rows),
        "unsupported": unsupported_prf(rows),
        "false_support_rate": false_support_rate(rows),
        "calibration": calibration(rows),
        "accuracy_by_change_type": accuracy_by_change_type(rows),
        "latency_per_claim": latency(rows),
    }
