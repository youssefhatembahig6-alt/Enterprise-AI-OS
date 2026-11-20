"""Run parser adapters over the fixtures, score them and write a results JSON.

    python -m bench.run --where laptop                 # all laptop adapters (no models)
    python -m bench.run --where colab                  # in the Colab notebook only
    python -m bench.run --where laptop --adapters pymupdf,pdfplumber --runs 5

Each adapter/fixture pair runs once cold, then ``--runs`` timed warm runs. The output of the
last run is scored and its text is saved under ``results/<run_id>/outputs`` for inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

# Opt out of Unstructured's anonymous usage ping and its console notice before any import.
os.environ.setdefault("DO_NOT_TRACK", "true")
os.environ.setdefault("SCARF_NO_ANALYTICS", "true")
os.environ.setdefault("UNSTRUCTURED_DISABLE_NOTICE", "1")

from bench.adapters import ADAPTERS, MODEL_MODULES
from bench.scoring import score

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixtures() -> tuple[dict, list[dict]]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    for e in manifest["fixtures"]:
        for key, rel in (("sha256", e["file"]), ("gold_sha256", e["gold"])):
            actual = sha256(FIXTURES / rel)
            if actual != e[key]:
                raise SystemExit(f"fixture hash mismatch for {rel}: manifest {e[key]}, file {actual}")
    return manifest, manifest["fixtures"]


def loaded_model_modules() -> list[str]:
    return sorted(m for m in MODEL_MODULES if m in sys.modules)


def environment(where: str) -> dict:
    env: dict = {
        "where": where,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                env["cpu"] = line.split(":", 1)[1].strip()
                break
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                env["ram_gib"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                break
    except OSError:
        pass
    try:
        q = "name,memory.total,driver_version"
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        env["gpu"] = out or None
    except (OSError, subprocess.SubprocessError):
        env["gpu"] = None
    return env


def versions(names: list[str]) -> dict:
    out = {}
    for n in sorted(set(names)):
        try:
            out[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            out[n] = None
    return out


def routing_check(fixtures: list[dict]) -> list[dict]:
    """Spec step 1: does a PDF have extractable text, or is it a scan? (PyMuPDF, no models)."""
    import pymupdf

    rows = []
    for e in fixtures:
        if e["media_type"] != "application/pdf":
            continue
        with pymupdf.open(FIXTURES / e["file"]) as doc:
            chars = sum(len(p.get_text("text").strip()) for p in doc)
            pages = len(doc)
            image_pages = sum(1 for p in doc if p.get_images())
        rows.append(
            {
                "fixture": e["id"],
                "case": e["case"],
                "pages": pages,
                "text_chars": chars,
                "pages_with_images": image_pages,
                "route": "ocr+layout" if chars < 20 * pages else "layout-aware text",
            }
        )
    return rows


def run(where: str, names: list[str] | None, runs: int, out_dir: Path, batch: str | None = None) -> Path:
    manifest, fixtures = load_fixtures()
    adapters = [a for a in ADAPTERS.values() if a.where == where and (names is None or a.name in names)]
    if names:
        unknown = set(names) - {a.name for a in adapters}
        if unknown:
            raise SystemExit(f"unknown adapters for --where {where}: {sorted(unknown)}")
    if where == "laptop" and loaded_model_modules():
        raise SystemExit(f"model runtime already imported: {loaded_model_modules()}")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    outputs = out_dir / run_id / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    results = []
    for a in adapters:
        for e in fixtures:
            if e["case"] not in a.cases:
                continue
            gold = json.loads((FIXTURES / e["gold"]).read_text(encoding="utf-8"))
            path = FIXTURES / e["file"]
            rec: dict = {"adapter": a.name, "fixture": e["id"], "case": e["case"], "runs": runs}
            print(f"[{a.name}] {e['id']} ...", flush=True)
            try:
                t0 = time.perf_counter()
                out = a.run(path)
                rec["cold_s"] = round(time.perf_counter() - t0, 4)
                times = []
                for _ in range(runs):
                    t0 = time.perf_counter()
                    out = a.run(path)
                    times.append(time.perf_counter() - t0)
                rec["warm_s"] = [round(t, 4) for t in times]
                rec["median_s"] = round(statistics.median(times), 4)
                rec["metrics"] = score(gold, out)
                rec["status"] = "ok"
                _save_output(outputs, a.name, e["id"], out)
            except Exception as exc:
                rec["status"] = "failed"
                rec["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                rec["traceback_tail"] = traceback.format_exc().strip().splitlines()[-3:]
            if where == "laptop":
                leaked = loaded_model_modules()
                rec["model_modules_loaded"] = leaked
                if leaked:
                    rec["status"] = "violation"
                    results.append(rec)
                    _write(out_dir, run_id, manifest, where, adapters, results, fixtures, batch)
                    raise SystemExit(f"{a.name} imported a model runtime: {leaked}")
            results.append(rec)
    return _write(out_dir, run_id, manifest, where, adapters, results, fixtures, batch)


def _save_output(outputs: Path, adapter: str, fixture: str, out: dict) -> None:
    (outputs / f"{adapter}__{fixture}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )


def _write(
    out_dir: Path,
    run_id: str,
    manifest: dict,
    where: str,
    adapters: list,
    results: list,
    fixtures: list,
    batch: str | None,
) -> Path:
    pkgs = [p for a in adapters for p in a.packages] + ["rapidfuzz"]
    doc = {
        "run_id": run_id,
        "batch": batch or run_id,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "environment": environment(where),
        "packages": versions(pkgs),
        "fixture_manifest_sha256": sha256(FIXTURES / "manifest.json"),
        "fixture_generator": manifest["generator"],
        "adapters": {
            a.name: {
                "label": a.label,
                "config": a.config,
                "uses_models": a.uses_models,
                "where": a.where,
                "notes": a.notes,
            }
            for a in adapters
        },
        "routing": routing_check(fixtures) if where == "laptop" else None,
        "results": results,
    }
    path = out_dir / f"{run_id}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--where", choices=("laptop", "colab"), default="laptop")
    ap.add_argument("--adapters", help="comma-separated subset")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, default=ROOT / "results")
    ap.add_argument("--batch", help="label shared by the runs of one notebook session (report merges them)")
    args = ap.parse_args(argv)
    names = args.adapters.split(",") if args.adapters else None
    run(args.where, names, args.runs, args.out, args.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
