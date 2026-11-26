"""Colab T4 compatibility smoke test for HHEM-2.1-open: the pinned model card's examples.

    python smoke_model_card.py --out /content/hhem-smoke-report.json

What it checks, with the merged scorer (hhem_score.py) and nothing else:
  1. the runtime is the supported Colab GPU runtime (Colab, torch, CUDA, Tesla T4);
  2. the installed transformers is exactly 4.56.2 (and the rest of requirements.txt);
  3. the pinned HHEM and flan-t5-base files download, verify by SHA-256, are staged into
     clean load directories and load offline through the reviewed code (``HhemBackend``);
  4. the seven public examples from the model card at the pinned revision score within
     TOLERANCE of the published ``consistent`` scores on every pass, with the same > 0.5
     decisions;
  5. the report records the GPU, driver, library versions, model load time and per-example
     latency (a warm-up pass, then REPEATS timed passes).

Public examples only: no M7 data is read or sent. Nothing is fine-tuned and no threshold is
chosen (0.5 is the model's own two-label argmax, as in the scorer).

Exit codes: 0 passed, 1 scores or decisions differ, 2 unsupported runtime or versions,
3 download, staging, model load or scoring error.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hhem_score as hhem  # the merged scorer, next to this file

SMOKE_VERSION = "1.1.0"
EXIT_RUN_ERROR = 3
# The model card (README.md) at hhem.HHEM_REVISION, SHA-256 below. "Using with `pipeline`"
# publishes these consistent-label scores for these pairs (the `predict()` example shows the
# same values rounded to 4 decimals).
MODEL_CARD_SHA256 = "85f87c95ca2ced124cc3a2f2ff27ea00874787d460b684ab12755b9eac0f677c"
EXAMPLES: tuple[tuple[str, str, float], ...] = (
    ("The capital of France is Berlin.", "The capital of France is Paris.", 0.011061512865126133),
    ("I am in California", "I am in United States.", 0.6473632454872131),
    ("I am in United States", "I am in California.", 0.1290171593427658),
    (
        "A person on a horse jumps over a broken down airplane.",
        "A person is outdoors, on a horse.",
        0.8969419002532959,
    ),
    (
        "A boy is jumping on skateboard in the middle of a red bridge.",
        "The boy skates down the sidewalk on a red bridge",
        0.18462494015693665,
    ),
    (
        "A man with blond-hair, and a brown shirt drinking out of a public water fountain.",
        "A blond man wearing a brown shirt is reading a book.",
        0.005031010136008263,
    ),
    ("Mark Wahlberg was a fan of Manny.", "Manny was a fan of Mark Wahlberg.", 0.05432349815964699),
)
# Absolute tolerance on P(consistent). The published values come from an unknown device and
# batch; float32 GPU kernels differ in the last digits, not at 5e-3.
TOLERANCE = 5e-3
REQUIRED_VERSIONS = {"transformers": "4.56.2", "tokenizers": "0.22.2", "safetensors": "0.8.0"}
REPEATS = 5


# ------------------------------------------------------------- pure, testable parts
def check_versions(version_of: Callable[[str], str | None]) -> dict[str, str | None]:
    """Installed versions; raises RuntimeUnsupported if a required one differs."""

    found = {name: version_of(name) for name in REQUIRED_VERSIONS}
    wrong = {n: v for n, v in found.items() if v != REQUIRED_VERSIONS[n]}
    if wrong:
        raise hhem.RuntimeUnsupported(f"need {REQUIRED_VERSIONS}, found {found}")
    return found


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def compare(actual: Sequence[float], tolerance: float = TOLERANCE) -> dict[str, Any]:
    if len(actual) != len(EXAMPLES):
        raise ValueError(f"expected {len(EXAMPLES)} scores, got {len(actual)}")
    rows = []
    for (premise, hypothesis, published), score in zip(EXAMPLES, actual, strict=True):
        valid_score = math.isfinite(score) and 0.0 <= score <= 1.0
        diff = abs(score - published) if valid_score else None
        rows.append(
            {
                "premise": premise,
                "hypothesis": hypothesis,
                "published": published,
                "actual": score if valid_score else None,
                "valid_score": valid_score,
                "abs_diff": diff,
                "within_tolerance": diff is not None and diff <= tolerance,
                "decision_matches": valid_score and (score > 0.5) == (published > 0.5),
            }
        )
    max_diff = max((r["abs_diff"] for r in rows if r["abs_diff"] is not None), default=None)
    ok = all(r["within_tolerance"] and r["decision_matches"] for r in rows)
    return {"tolerance": tolerance, "max_abs_diff": max_diff, "passed": ok, "examples": rows}


def latency_summary(values_ms: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values_ms)
    p95_rank = max(1, -(-95 * len(ordered) // 100))  # nearest rank, ceil(0.95 n)
    return {
        "n": len(ordered),
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[p95_rank - 1],
        "mean_ms": statistics.fmean(ordered),
        "max_ms": ordered[-1],
    }


def _driver() -> str | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_commit() -> str | None:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or None


# ----------------------------------------------------------------------------- run
def _score_pass(backend: hhem.Backend) -> tuple[list[float], list[float]]:
    scores, times = [], []
    for premise, hypothesis, _ in EXAMPLES:
        hhem._sync(backend)
        t0 = time.perf_counter()
        scores.append(backend.score(premise, hypothesis))
        hhem._sync(backend)
        times.append((time.perf_counter() - t0) * 1000)
    return scores, times


def _summarize_passes(backend: hhem.Backend, report: dict[str, Any]) -> None:
    first_scores, first_times = _score_pass(backend)  # warm-up (CUDA kernels, allocator)
    report["first_pass_latency_ms"] = first_times
    comparison = compare(first_scores)
    report["comparison"] = comparison
    for example, ms in zip(comparison["examples"], first_times, strict=True):
        example["warmup_latency_ms"] = ms
        example["timed_passes"] = []
    timed_latencies = []
    for attempt in range(1, REPEATS + 1):
        scores, times = _score_pass(backend)
        measured = compare(scores)
        if scores != first_scores:
            report["nondeterministic_scores"] = True
        comparison["passed"] = comparison["passed"] and measured["passed"]
        if measured["max_abs_diff"] is not None and (
            comparison["max_abs_diff"] is None
            or measured["max_abs_diff"] > comparison["max_abs_diff"]
        ):
            comparison["max_abs_diff"] = measured["max_abs_diff"]
        for example, sample, ms in zip(
            comparison["examples"], measured["examples"], times, strict=True
        ):
            example["timed_passes"].append(
                {
                    "pass": attempt,
                    "latency_ms": ms,
                    "actual": sample["actual"],
                    "valid_score": sample["valid_score"],
                    "abs_diff": sample["abs_diff"],
                    "within_tolerance": sample["within_tolerance"],
                    "decision_matches": sample["decision_matches"],
                }
            )
        timed_latencies.extend(times)
    report["latency_per_example"] = {
        **latency_summary(timed_latencies),
        "repeats": REPEATS,
        "excludes": "warm-up pass and model load",
    }


def run(cache_dir: Path) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "kind": "m7-hhem-model-card-smoke",
        "smoke_version": SMOKE_VERSION,
        "model": hhem.HHEM_REPO,
        "revision": hhem.HHEM_REVISION,
        "model_card_sha256": MODEL_CARD_SHA256,
        "foundation": {"repo": hhem.FLAN_REPO, "revision": hhem.FLAN_REVISION},
        "code_commit": _git_commit(),
        "scorer_sha256": hhem._sha((Path(hhem.__file__)).read_bytes()),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        report["runtime"] = hhem.check_runtime()  # before any download
        report["versions"] = check_versions(installed_version)
    except hhem.RuntimeUnsupported as exc:
        report.update(status="unsupported", reason=str(exc))
        return hhem.EXIT_UNSUPPORTED_RUNTIME, report
    report["nvidia_driver"] = _driver()
    stage = "download"
    try:
        hhem_src, flan_src = hhem.fetch(cache_dir)
        stage = "staging"
        hhem_dir, flan_dir = hhem.stage_all(cache_dir, hhem_src, flan_src)
        stage = "model-load"
        started = time.perf_counter()
        backend = hhem.HhemBackend(hhem_dir, flan_dir, "cuda")
        report["load_seconds"] = time.perf_counter() - started
        report["backend"] = backend.describe()
        stage = "scoring"
        _summarize_passes(backend, report)
    except Exception as exc:
        report.update(status="error", stage=stage, error_type=type(exc).__name__, reason=str(exc))
        return EXIT_RUN_ERROR, report
    report["status"] = "passed" if report["comparison"]["passed"] else "failed"
    return (0 if report["status"] == "passed" else 1), report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path("/content/hhem-pinned"))
    args = ap.parse_args(argv)
    args.out.unlink(missing_ok=True)
    code, report = run(args.cache_dir)
    with tempfile.TemporaryDirectory(dir=args.out.parent, prefix=".hhem-smoke-") as directory:
        output = Path(directory) / "report.json"
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", "utf-8"
        )
        output.replace(args.out)
    summary = {k: report.get(k) for k in ("status", "stage", "reason", "load_seconds")}
    if "latency_per_example" in report:
        summary["max_abs_diff"] = report["comparison"]["max_abs_diff"]
        summary["latency_p50_ms"] = report["latency_per_example"]["p50_ms"]
        summary["gpu"] = report["runtime"].get("device_name")
    print(json.dumps({k: v for k, v in summary.items() if v is not None}, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
