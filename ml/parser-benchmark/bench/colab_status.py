"""Stage tracking and the partial-run summary for the Colab notebook.

Standard library only: the notebook imports this with Colab's system Python before (and
whether or not) either model environment installs.

Each notebook session has one batch id. Stages are recorded per environment in
``results/<batch>/status.json``::

    {"envs": {"docling": {"install": {...}, "run": {...}}, "unstructured": {...}},
     "report": {...}}

A stage record is ``{"ok": bool, "returncode": int | None, "detail": str, "log": str | None}``.

``summarize`` classifies every expected Colab adapter/fixture pair for the batch:

* ``succeeded``: a result with status ``ok`` exists in a results file of this batch;
* ``failed``: the pair's stage failed. The stage is named: ``install`` (the environment did not
  install), ``runner`` (the runner exited non-zero before reaching the pair), or ``parse``
  (the runner reached the pair and the parser raised);
* ``missing``: no result and no recorded failure (for example, the run cell never ran).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from bench.adapters import ADAPTERS

# Which Colab environment runs which adapters (see build_notebook.py and colab-*-pins.txt).
ENV_ADAPTERS: dict[str, list[str]] = {
    "docling": ["docling", "pymupdf_layout"],
    "unstructured": ["unstructured_fast", "unstructured_hi_res", "unstructured_ocr_only", "unstructured_xlsx"],
}

ROOT = Path(__file__).resolve().parents[1]


def expected_pairs(fixtures: list[dict] | None = None) -> list[tuple[str, str, str]]:
    """(environment, adapter, fixture) for every Colab adapter and applicable fixture."""
    if fixtures is None:
        fixtures = json.loads((ROOT / "fixtures/manifest.json").read_text(encoding="utf-8"))["fixtures"]
    pairs = []
    for env, names in ENV_ADAPTERS.items():
        for name in names:
            a = ADAPTERS[name]
            assert a.where == "colab", name
            pairs.extend((env, name, f["id"]) for f in fixtures if f["case"] in a.cases)
    return pairs


class Status:
    """status.json for one batch; every update is written to disk immediately."""

    def __init__(self, batch_dir: Path | str) -> None:
        self.dir = Path(batch_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "status.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"envs": {env: {} for env in ENV_ADAPTERS}, "report": None}
            self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1) + "\n", encoding="utf-8")

    def record(
        self,
        env: str | None,
        stage: str,
        ok: bool,
        returncode: int | None = None,
        detail: str = "",
        log: str | None = None,
    ) -> dict:
        rec = {
            "ok": bool(ok),
            "returncode": returncode,
            "detail": detail[-2000:],
            "log": log,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if env is None:
            self.data[stage] = rec
        else:
            self.data["envs"].setdefault(env, {})[stage] = rec
        self._save()
        return rec

    def stage(self, env: str, stage: str) -> dict | None:
        return self.data["envs"].get(env, {}).get(stage)


def batch_results(results_dir: Path | str, batch: str) -> dict[tuple[str, str], dict]:
    """Latest record per (adapter, fixture) from the colab results files of ``batch``."""
    found: dict[tuple[str, str], dict] = {}
    for p in sorted(Path(results_dir).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a truncated file is reported by the runner stage, not here
        if not isinstance(d, dict) or d.get("batch") != batch or d.get("environment", {}).get("where") != "colab":
            continue
        for rec in d.get("results", []):
            found[(rec["adapter"], rec["fixture"])] = rec | {"_file": p.name}
    return found


def summarize(status: Status, results_dir: Path | str, batch: str, fixtures: list[dict] | None = None) -> dict:
    results = batch_results(results_dir, batch)
    rows = []
    for env, adapter, fixture in expected_pairs(fixtures):
        rec = results.get((adapter, fixture))
        install, run = status.stage(env, "install"), status.stage(env, "run")
        if rec is not None and rec.get("status") == "ok":
            state, stage, detail = "succeeded", None, ""
        elif rec is not None:
            state, stage, detail = "failed", "parse", rec.get("error", rec.get("status", ""))
        elif install is not None and not install["ok"]:
            state, stage, detail = "failed", "install", install["detail"]
        elif run is not None and not run["ok"]:
            state, stage, detail = "failed", "runner", f"exit code {run['returncode']}: {run['detail']}"
        else:
            state, stage, detail = "missing", None, "no result and no recorded failure"
        rows.append(
            {
                "env": env,
                "adapter": adapter,
                "fixture": fixture,
                "state": state,
                "stage": stage,
                "detail": detail[-300:],
            }
        )
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("succeeded", "failed", "missing")}
    return {"batch": batch, "expected": len(rows), "counts": counts, "pairs": rows, "stages": status.data}


def summary_markdown(summary: dict) -> str:
    c = summary["counts"]
    lines = [
        f"# Colab batch `{summary['batch']}`",
        "",
        f"{summary['expected']} expected pairs: {c['succeeded']} succeeded, {c['failed']} failed, "
        f"{c['missing']} missing.",
        "",
        "## Stages",
        "",
        "| Environment | Stage | Result | Exit | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for env, stages in summary["stages"]["envs"].items():
        for name in ("install", "run"):
            s = stages.get(name)
            if s is None:
                lines.append(f"| {env} | {name} | not run | — | |")
            else:
                detail = s["detail"].splitlines()[-1][:120] if s["detail"] else ""
                lines.append(f"| {env} | {name} | {'ok' if s['ok'] else 'FAILED'} | {s['returncode']} | {detail} |")
    rep = summary["stages"].get("report")
    lines.append(
        f"| — | report | {'not run' if rep is None else ('ok' if rep['ok'] else 'FAILED')} | "
        f"{'—' if rep is None else rep['returncode']} | |"
    )
    lines += [
        "",
        "## Pairs",
        "",
        "| Environment | Adapter | Fixture | State | Failed stage | Detail |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in summary["pairs"]:
        detail = r["detail"].replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(f"| {r['env']} | {r['adapter']} | {r['fixture']} | {r['state']} | {r['stage'] or ''} | {detail} |")
    return "\n".join(lines) + "\n"


def write_summary(status: Status, results_dir: Path | str, batch: str) -> dict:
    s = summarize(status, results_dir, batch)
    (status.dir / "summary.json").write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
    (status.dir / "summary.md").write_text(summary_markdown(s), encoding="utf-8")
    return s


def run_report(
    status: Status,
    bench_dir: Path | str,
    batch: str,
    results_dir: Path | str | None = None,
    python: str = sys.executable,
    env: dict[str, str] | None = None,
) -> Path | None:
    """Build the tables for this batch into ``results/<batch>/results-tables.md``.

    Returns the path only when the report command succeeded and wrote that file; otherwise the
    failure is recorded as the ``report`` stage and ``None`` is returned, so the notebook never
    shows tables from an earlier batch or the committed repository copy.
    """
    bench_dir = Path(bench_dir)
    out = status.dir / "results-tables.md"
    if out.exists():
        out.unlink()
    results_dir = Path(results_dir) if results_dir is not None else bench_dir / "results"
    cmd = [python, "-m", "bench.report", "--results-dir", str(results_dir), "--colab-batch", batch, "--out", str(out)]
    try:
        p = subprocess.run(cmd, cwd=bench_dir, capture_output=True, text=True, timeout=600, env=env)
        rc, text = p.returncode, (p.stdout + p.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        rc, text = None, f"{type(exc).__name__}: {exc}"
    ok = rc == 0 and out.exists() and f"batch `{batch}`" in out.read_text(encoding="utf-8")
    status.record(None, "report", ok, rc, text if not ok else "", log=None)
    return out if ok else None
