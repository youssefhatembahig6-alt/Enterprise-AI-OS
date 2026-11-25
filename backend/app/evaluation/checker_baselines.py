"""Checker baseline runner: score the specified baselines on human-reviewed dev labels.

Baselines (Member 5 report, section 4.3):
  deberta      the current pinned checker, ``MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli``,
               which is the specification's "zero-shot NLI checkpoint (no fine-tuning)";
               scored through the model gateway ``/check`` exactly as the app calls it
  qwen-judge   Qwen3-8B as a judge, through the gateway chat route (temperature 0,
               thinking off, fixed versioned prompt)
  predictions  scores produced elsewhere (e.g. HHEM-2.1-open on Colab), read from a JSONL
               file whose header names the model, revision and runtime

Evaluation data. The only labels used are **human** gold labels for the M7 gold checker
pairs: pairs that two annotators labelled the same way, or that were adjudicated
(``labels.human_gold``). Every pair must be in the dev split, and every source it cites
must be a dev member of the active frozen manifest, selected from frozen metadata by
``loader.eligible("model-selection")``. Silver labels, AI-proposed labels, train data,
held-out data and the blind release set are never read. Without human-reviewed pairs the
run is **blocked**: it reports why and exits before contacting any model.

Operating points. No threshold is selected here (dev threshold selection is a later step).
DeBERTa is reported at the configured ``CHECKER_ENTAILMENT_THRESHOLD`` (default 0.5,
documented as uncalibrated). The judge and the predictions file report their own
decisions. Threshold-free calibration is reported for every scorer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from app.evaluation import labels, loader
from app.evaluation.checker_metrics import SUPPORTED, UNSUPPORTED, Scored, summarize
from app.evaluation.pilot import GENERATED
from app.reporting.model_client import ModelClient

JUDGE_PROMPT_VERSION = "m7-judge-1"
JUDGE_SYSTEM = (
    "You check whether a claim is fully supported by a passage. Use only the passage. "
    "Every number, date, name, unit, scope word and polarity must match. "
    "Answer with exactly one word: SUPPORTED or UNSUPPORTED."
)
EXIT_BLOCKED = 3


class SplitViolation(ValueError):
    """An evaluation pair is not a dev pair, or cites a non-dev source."""


@dataclass(frozen=True)
class EvalPair:
    pair_id: str
    claim: str
    premise: str
    gold: str
    change_type: str
    family: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def premise_of(pair: dict[str, Any]) -> str:
    title, evidence = pair["evidence_title"], pair["evidence"]
    return title if evidence == title else f"{title}\n\n{evidence}"


def dev_eval_set(
    label_dir: Path = labels.LABEL_DIR, m7_dir: Path = loader.M7_DIR
) -> tuple[list[EvalPair], dict[str, Any]]:
    """Human-reviewed dev pairs only, each checked against the frozen dev selection."""

    gold, problems = labels.human_gold(label_dir)
    if problems:
        raise ValueError(f"label files are invalid: {problems}")
    dev_refs = {e.ref for e in loader.eligible(loader.Purpose.MODEL_SELECTION, m7_dir)}
    pairs = labels.gold_pairs()
    out: list[EvalPair] = []
    for rec in pairs:
        if rec["split"] != "dev" or rec.get("release") is not False:
            raise SplitViolation(f"{rec['id']} is {rec['split']}, not a dev pilot pair")
        outside = sorted(set(rec["sources"]) - dev_refs)
        if outside:
            raise SplitViolation(f"{rec['id']} cites non-dev sources {outside}")
        if rec["id"] not in gold:
            continue  # not (yet) human-reviewed: never scored
        out.append(
            EvalPair(
                pair_id=rec["id"],
                claim=rec["claim"],
                premise=premise_of(rec),
                gold=gold[rec["id"]],
                change_type=rec["kind"],
                family=rec["family"],
            )
        )
    status = labels.status(label_dir)
    snaps = json.loads((m7_dir / "frozen-snapshots.json").read_text("utf-8"))
    entry = snaps["manifests"][snaps["active"]]
    label_files = sorted(label_dir.glob("*.csv")) if label_dir.is_dir() else []
    info = {
        "source": "M7 pilot gold checker pairs (dev split)",
        "pairs_file_sha256": _sha((GENERATED / "gold-checker-pairs.jsonl").read_bytes()),
        "dataset_revision": labels.dataset_revision(),
        "manifest": snaps["active"],
        "manifest_sha256": entry["manifest_sha256"],
        "label_files_sha256": {p.name: _sha(p.read_bytes()) for p in label_files},
        "candidate_pairs": len(pairs),
        "human_reviewed_pairs": len(out),
        "human_label_states": status["state_counts"],
        "cohen_kappa": status["cohen_kappa"],
        "gold": {
            SUPPORTED: sum(p.gold == SUPPORTED for p in out),
            UNSUPPORTED: sum(p.gold == UNSUPPORTED for p in out),
        },
        "label_source": "human (two agreeing annotators or adjudication); never silver or AI",
    }
    return out, info


# ---------------------------------------------------------------------- scorers
class Scorer(Protocol):
    name: str

    def describe(self) -> dict[str, Any]: ...

    def score(self, pair: EvalPair) -> tuple[float, str, float, dict[str, Any]]:
        """(p_supported, predicted label, latency ms, extra)."""
        ...


class DebertaGatewayScorer:
    name = "deberta-zero-shot-nli"

    def __init__(self, client: ModelClient, threshold: float) -> None:
        self.client, self.threshold = client, threshold

    def describe(self) -> dict[str, Any]:
        return {
            "baseline": "zero-shot NLI checkpoint (current pinned checker, no fine-tuning)",
            "model": self.client.describe().get("checker"),
            "served": self.client.served_models().summary(),
            "operating_point": {
                "rule": "supported if P(entailment) >= CHECKER_ENTAILMENT_THRESHOLD",
                "threshold": self.threshold,
                "provenance": "configured default (uncalibrated, M1); not selected here",
            },
            "p_supported": "P(entailment) from the gateway /check",
        }

    def score(self, pair: EvalPair) -> tuple[float, str, float, dict[str, Any]]:
        started = time.perf_counter()
        result = self.client.check([(pair.premise, pair.claim)])[0]
        ms = (time.perf_counter() - started) * 1000
        p = result.entailment
        label = SUPPORTED if p >= self.threshold else UNSUPPORTED
        return p, label, ms, {"neutral": result.neutral, "contradiction": result.contradiction}


def parse_judge(text: str) -> str | None:
    word = text.strip().split()[0].strip(".:,*").upper() if text.strip() else ""
    if word in ("UNSUPPORTED", "NOT"):
        return UNSUPPORTED
    if word == "SUPPORTED":
        return SUPPORTED
    return None


class QwenJudgeScorer:
    name = "qwen3-8b-judge"

    def __init__(self, client: ModelClient) -> None:
        self.client = client

    def describe(self) -> dict[str, Any]:
        return {
            "baseline": "Qwen3-8B as a judge (prompted, no fine-tuning)",
            "model": self.client.describe().get("chat"),
            "served": self.client.served_models().summary(),
            "prompt_version": JUDGE_PROMPT_VERSION,
            "prompt_sha256": _sha(JUDGE_SYSTEM.encode()),
            "decoding": "temperature 0, thinking off, max 8 tokens",
            "operating_point": {"rule": "the judge's own one-word answer", "threshold": None},
            "p_supported": "1.0 for SUPPORTED, 0.0 otherwise (hard labels)",
            "unparseable": "scored UNSUPPORTED (fail closed) and counted",
        }

    def score(self, pair: EvalPair) -> tuple[float, str, float, dict[str, Any]]:
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"Passage:\n{pair.premise}\n\nClaim:\n{pair.claim}"},
        ]
        started = time.perf_counter()
        result = self.client.chat(messages, purpose="answer", max_tokens=8)
        ms = (time.perf_counter() - started) * 1000
        label = parse_judge(result.text)
        extra = {"raw": result.text[:40], "unparseable": label is None}
        label = label or UNSUPPORTED
        return (1.0 if label == SUPPORTED else 0.0), label, ms, extra


class PredictionsScorer:
    """Scores computed elsewhere (HHEM-2.1-open on Colab). First line: a header object with
    ``model``, ``revision`` and ``runtime``; then one object per pair with ``pair_id``,
    ``p_supported``, ``predicted`` and ``latency_ms``."""

    name = "predictions-file"

    def __init__(self, path: Path, expected_ids: Sequence[str]) -> None:
        lines = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        if not lines:
            raise ValueError(f"{path} is empty")
        self.header, rows = lines[0], lines[1:]
        missing_meta = [k for k in ("model", "revision", "runtime") if not self.header.get(k)]
        if missing_meta:
            raise ValueError(f"{path} header lacks {missing_meta}")
        self.rows = {r["pair_id"]: r for r in rows}
        if set(self.rows) != set(expected_ids) or len(rows) != len(self.rows):
            raise ValueError(f"{path} must score exactly the evaluation pairs, once each")
        self.sha256 = _sha(path.read_bytes())

    def describe(self) -> dict[str, Any]:
        return {"baseline": self.header.get("baseline", "external predictions"),
                "model": {"model": self.header["model"], "revision": self.header["revision"]},
                "runtime": self.header["runtime"], "file_sha256": self.sha256,
                "operating_point": self.header.get("operating_point")}  # fmt: skip

    def score(self, pair: EvalPair) -> tuple[float, str, float, dict[str, Any]]:
        row = self.rows[pair.pair_id]
        return float(row["p_supported"]), row["predicted"], float(row["latency_ms"]), {}


# ----------------------------------------------------------------------- runner
def _git_commit() -> str | None:
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=loader.BACKEND,
        capture_output=True,
        check=False,
    )
    return done.stdout.decode().strip() or None


def runtime_info() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "code_commit": _git_commit(),
        "runner": "app/evaluation/checker_baselines.py",
    }


def score_all(scorer: Scorer, pairs: Sequence[EvalPair]) -> list[tuple[Scored, dict[str, Any]]]:
    out = []
    for pair in pairs:
        p, label, ms, extra = scorer.score(pair)
        out.append((Scored(pair.pair_id, pair.gold, p, label, ms, pair.change_type), extra))
    return out


def run(
    scorer_factory: Callable[[], Scorer],
    pairs: Sequence[EvalPair],
    dataset: dict[str, Any],
    *,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Score and report. With no human-reviewed pairs the run is blocked and the scorer
    (and so the model gateway) is never created."""

    report: dict[str, Any] = {
        "kind": "m7-checker-baseline",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "dataset": dataset,
        "runtime": runtime_info(),
    }
    if synthetic:
        report["status"] = "synthetic-selftest"
        report["not_a_result"] = "synthetic inputs exercise the runner only; no model was scored"
    if not pairs:
        report["status"] = "blocked"
        report["reason"] = (
            f"0 of {dataset.get('candidate_pairs', 0)} dev gold checker pairs have human gold "
            "labels (two agreeing annotators or adjudication); scoring needs human-reviewed "
            "dev labels"
        )
        return report
    scorer = scorer_factory()
    scored = score_all(scorer, pairs)
    report.setdefault("status", "scored")
    report["scorer"] = {"name": scorer.name, **scorer.describe()}
    report["metrics"] = summarize([s for s, _ in scored])
    report["predictions"] = [
        {
            "pair_id": s.pair_id,
            "gold": s.gold,
            "p_supported": s.p_supported,
            "predicted": s.predicted,
            "latency_ms": s.latency_ms,
            "change_type": s.change_type,
            **extra,
        }
        for s, extra in scored
    ]
    if scorer.name == "qwen3-8b-judge":
        report["metrics"]["unparseable"] = sum(bool(e.get("unparseable")) for _, e in scored)
    return report


# ---------------------------------------------------------------- synthetic self-test
def synthetic_pairs() -> list[EvalPair]:
    """Twelve made-up pairs with known labels. They are not M7 data."""

    golds = [SUPPORTED] * 5 + [UNSUPPORTED] * 7
    kinds = ["original"] * 5 + ["number", "number", "entity", "entity", "scope", "polarity",
                                "wrong_evidence"]  # fmt: skip
    return [
        EvalPair(
            f"synthetic-{i:02d}", f"Synthetic claim {i}.", f"Synthetic premise {i}.", g, k, "s"
        )
        for i, (g, k) in enumerate(zip(golds, kinds, strict=True))
    ]


class SyntheticScorer:
    """Deterministic fake: fixed probabilities and latencies, so every metric is known."""

    name = "synthetic-fake"
    P: ClassVar[list[float]] = [0.95, 0.9, 0.8, 0.7, 0.3, 0.6, 0.4, 0.2, 0.1, 0.05, 0.55, 0.15]
    LATENCY: ClassVar[list[float]] = [
        10.0, 12.0, 11.0, 30.0, 9.0, 14.0, 13.0, 10.0, 50.0, 12.0, 11.0, 10.0
    ]  # fmt: skip

    def __init__(self) -> None:
        self.calls = 0

    def describe(self) -> dict[str, Any]:
        return {"model": None, "operating_point": {"threshold": 0.5, "provenance": "synthetic"}}

    def score(self, pair: EvalPair) -> tuple[float, str, float, dict[str, Any]]:
        i = int(pair.pair_id.rsplit("-", 1)[1])
        self.calls += 1
        p = self.P[i]
        return p, SUPPORTED if p >= 0.5 else UNSUPPORTED, self.LATENCY[i], {}


def synthetic_selftest() -> dict[str, Any]:
    pairs = synthetic_pairs()
    dataset = {"source": "synthetic (runner self-test only)", "candidate_pairs": len(pairs),
               "human_reviewed_pairs": 0}  # fmt: skip
    return run(SyntheticScorer, pairs, dataset, synthetic=True)
