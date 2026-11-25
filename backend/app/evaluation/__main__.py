"""M7 evaluation commands.

    python -m app.evaluation freeze              write the split-manifest lock (once)
    python -m app.evaluation check               deterministic CI smoke check
    python -m app.evaluation build-pilot         regenerate the pilot datasets
    python -m app.evaluation report [--json]     dataset counts and label status
    python -m app.evaluation labels export --annotator ID [--out PATH]
    python -m app.evaluation labels status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.evaluation import manifest as mf


def cmd_freeze(_: argparse.Namespace) -> int:
    manifest, problems = mf.load_manifest()
    if problems:
        print("\n".join(f"manifest: {p}" for p in problems), file=sys.stderr)
        return 1
    if mf.LOCK.exists():
        print(f"{mf.LOCK.name} already exists: a frozen manifest is never re-frozen", file=sys.stderr)
        return 1
    mf.LOCK.write_text(json.dumps(mf.build_lock(manifest), indent=2, sort_keys=True) + "\n")
    print(f"froze {manifest.name} sha256={manifest.sha256}")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    from app.evaluation.checks import run_checks

    problems, summary = run_checks()
    print(summary)
    if problems:
        print(f"\nM7 check FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        print("\n".join(f"  - {p}" for p in problems), file=sys.stderr)
        return 1
    print("\nM7 check passed")
    return 0


def cmd_build_pilot(_: argparse.Namespace) -> int:
    from app.evaluation.pilot import write_pilot

    for path, count in write_pilot().items():
        print(f"wrote {path} ({count} records)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from app.evaluation.checks import counts_report

    report = counts_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        from app.evaluation.checks import format_report

        print(format_report(report))
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    from app.evaluation import labels

    if args.action == "export":
        if not args.annotator:
            print("--annotator is required", file=sys.stderr)
            return 2
        out = Path(args.out) if args.out else None
        path = labels.export_sheet(args.annotator, out)
        print(f"wrote blind label sheet for {args.annotator}: {path}")
        return 0
    print(json.dumps(labels.status(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze").set_defaults(func=cmd_freeze)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("build-pilot").set_defaults(func=cmd_build_pilot)
    report = sub.add_parser("report")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)
    lab = sub.add_parser("labels")
    lab.add_argument("action", choices=["export", "status"])
    lab.add_argument("--annotator")
    lab.add_argument("--out")
    lab.set_defaults(func=cmd_labels)
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
