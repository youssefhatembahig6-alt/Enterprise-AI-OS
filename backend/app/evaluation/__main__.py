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

from app.evaluation import freeze, labels
from app.evaluation import manifest as mf
from app.evaluation.checks import counts_report, format_report, run_checks
from app.evaluation.pilot import write_pilot


def cmd_freeze(args: argparse.Namespace) -> int:
    """Write the lock for a NEW manifest file and print its snapshot entry for review.

    It never writes frozen-snapshots.json and never overwrites a lock.
    """

    manifest_file = args.manifest
    lock_file = manifest_file.removesuffix(".toml") + ".lock.json"
    manifest, problems = mf.load_manifest(mf.M7 / manifest_file)
    if problems:
        print("\n".join(f"manifest: {p}" for p in problems), file=sys.stderr)
        return 1
    lock = mf.M7 / lock_file
    if lock.exists():
        print(f"{lock_file} exists: a frozen manifest is never re-frozen", file=sys.stderr)
        return 1
    lock.write_text(mf.lock_text(mf.build_lock(manifest, manifest_file)))
    print(f"froze {manifest.name}: wrote {lock_file}")
    print("Add this entry to frozen-snapshots.json by hand, for independent review:")
    print(json.dumps(freeze.snapshot_entry(manifest_file, lock_file), indent=2))
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    problems, summary = run_checks()
    print(summary)
    if problems:
        print(f"\nM7 check FAILED ({len(problems)} problem(s)):", file=sys.stderr)
        print("\n".join(f"  - {p}" for p in problems), file=sys.stderr)
        return 1
    print("\nM7 check passed")
    return 0


def cmd_build_pilot(_: argparse.Namespace) -> int:
    for path, count in write_pilot().items():
        print(f"wrote {path} ({count} records)")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    report = counts_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(format_report(report))
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
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
    frz = sub.add_parser("freeze")
    frz.add_argument("manifest", help="new manifest file name, e.g. split-manifest-v3.toml")
    frz.set_defaults(func=cmd_freeze)
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
