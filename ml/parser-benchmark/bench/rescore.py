"""Re-apply the current scorer to the parser outputs saved with a run.

    python -m bench.rescore results/<run_id>.json --note "why the scorer changed"

Parsers are not rerun and timings are kept. Every metric that changes is listed under
``rescored`` in the results file, so the history of a score stays visible.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from bench.scoring import score

ROOT = Path(__file__).resolve().parents[1]


def _flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def rescore(path: Path, note: str) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    outputs = path.with_suffix("") / "outputs"
    changes = []
    for rec in doc["results"]:
        if rec.get("status") != "ok":
            continue
        out_file = outputs / f"{rec['adapter']}__{rec['fixture']}.json"
        out = json.loads(out_file.read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "fixtures/manifest.json").read_text(encoding="utf-8"))
        entry = next(e for e in manifest["fixtures"] if e["id"] == rec["fixture"])
        gold = json.loads((ROOT / "fixtures" / entry["gold"]).read_text(encoding="utf-8"))
        new = score(gold, out)
        old_f, new_f = _flat(rec["metrics"]), _flat(new)
        for key in sorted(set(old_f) | set(new_f)):
            if old_f.get(key, "<absent>") != new_f.get(key, "<absent>"):
                changes.append(
                    {
                        "adapter": rec["adapter"],
                        "fixture": rec["fixture"],
                        "metric": key,
                        "old": old_f.get(key, "<absent>"),
                        "new": new_f.get(key, "<absent>"),
                    }
                )
        rec["metrics"] = new
    doc.setdefault("rescored", []).append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": note,
            "changes": changes,
        }
    )
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--note", required=True)
    args = ap.parse_args(argv)
    changes = rescore(args.path, args.note)
    for c in changes:
        print(f"{c['adapter']} × {c['fixture']}: {c['metric']} {c['old']!r} -> {c['new']!r}")
    print(f"{len(changes)} metric(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
