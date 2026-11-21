"""Score PDFs as ingested by the application against the M1 gold references (M3).

    python -m bench.score_app app-parser-export.json --out app-parser-scores.json

``app-parser-export.json`` comes from ``python -m app.cli parser-export``: each PDF
version's stored canonical text, its table elements (rows split on `` | ``) and its
heading elements. The M1 scorer (``bench.scoring.score_pdf``) is applied unchanged and
the result is set beside M1's Docling scores for the same fixture (run
``20261120T220551Z-200026``), so a regression introduced by the integration (element
conversion, normalisation, furniture handling) is visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from bench.scoring import score_pdf

ROOT = Path(__file__).resolve().parents[1]
M1_RUN = ROOT / "results" / "20261120T220551Z-200026.json"
KEYS = (
    "text_similarity",
    "word_recall",
    "reading_order",
    "table_cells",
    "table_headers",
    "table_values_in_text",
    "heading_recall",
)


def _gold_by_sha() -> dict[str, tuple[str, dict[str, Any]]]:
    manifest = json.loads((ROOT / "fixtures" / "manifest.json").read_text(encoding="utf-8"))
    out = {}
    for fixture in manifest["fixtures"]:
        if not fixture["file"].endswith(".pdf"):
            continue
        gold = json.loads((ROOT / "fixtures" / fixture["gold"]).read_text(encoding="utf-8"))
        out[fixture["sha256"]] = (fixture["id"], gold)
    return out


def _m1_docling() -> dict[str, dict[str, Any]]:
    run = json.loads(M1_RUN.read_text(encoding="utf-8"))
    return {r["fixture"]: r["metrics"] for r in run["results"] if r["adapter"] == "docling" and r.get("status") == "ok"}


def score_export(export: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gold, m1 = _gold_by_sha(), _m1_docling()
    rows = []
    for item in export:
        match = gold.get(item["byte_sha256"])
        if match is None:
            continue
        fixture, reference = match
        row = {
            "fixture": fixture,
            "title": item["title"],
            "version": item["version"],
            "status": item["status"],
            "parser": item["parser"],
            "parse_notes": item["parse_notes"],
            "text_sha256": item["text_sha256"],
            "gold_sha256": hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest(),
        }
        if "out" in item:
            metrics = score_pdf(reference, item["out"])
            row["metrics"] = {k: metrics.get(k) for k in KEYS if k in metrics}
            row["m1_docling"] = {k: m1.get(fixture, {}).get(k) for k in row["metrics"]}
        rows.append(row)
    return rows


def markdown(rows: list[dict[str, Any]]) -> str:
    lines = [
        "| Fixture | Title | Status | Parser | " + " | ".join(KEYS) + " |",
        "| --- | --- | --- | --- | " + " | ".join("---" for _ in KEYS) + " |",
    ]

    def pct(value: Any) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}"

    for r in rows:
        metrics, ref = r.get("metrics", {}), r.get("m1_docling", {})
        cells = [f"{pct(metrics.get(k))} (M1 {pct(ref.get(k))})" if k in metrics else "—" for k in KEYS]
        lines.append(f"| {r['fixture']} | {r['title']} | {r['status']} | {r['parser']} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("export")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rows = score_export(json.loads(Path(args.export).read_text(encoding="utf-8")))
    Path(args.out).write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(markdown(rows))


if __name__ == "__main__":
    main()
