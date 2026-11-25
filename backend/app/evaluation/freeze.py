"""Enforce the freeze. Locks alone are not enough, because whoever edits a manifest can rerun
``freeze`` and ``build-pilot`` too.

Three independent anchors:

1. ``frozen-snapshots.json``, the reviewed snapshot, pins every manifest and lock by SHA-256.
   No tool writes it: ``freeze`` only prints the entry for a reviewer to add by hand. A
   manifest edit followed by a regenerated lock and pilot still mismatches the snapshot.
2. Against the base branch (CI: ``--base origin/main``), every frozen manifest, lock and
   snapshot entry that already exists on ``main`` must be unchanged. The only allowed
   transition is ``active`` -> ``superseded``. A PR cannot rewrite a reviewed snapshot
   without the diff showing in review, and once merged it cannot rewrite it at all.
3. In git history (CI: ``--git-history``), the commit that added the active lock must be a
   strict ancestor of the latest commit that changed the pilot index, so the freeze comes
   before the derived data.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.evaluation.manifest import (
    M7,
    REPO,
    Manifest,
    audit_exposure,
    parse_manifest,
    verify_lock,
)

M7_REL = M7.relative_to(REPO).as_posix()
SNAPSHOT_FILE = "frozen-snapshots.json"
IMMUTABLE_FIELDS = ("manifest", "lock", "manifest_sha256", "lock_sha256", "frozen_at")

Reader = Callable[[str], str | None]


def disk_reader(overlay: Mapping[str, str] | None = None) -> Reader:
    """Read files under the M7 fixture directory, with optional in-memory replacements."""

    def read(rel: str) -> str | None:
        if overlay and rel in overlay:
            return overlay[rel]
        path = M7 / rel
        return path.read_text("utf-8") if path.is_file() else None

    return read


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_snapshot(read: Reader) -> tuple[Manifest | None, list[str]]:
    """Verify every registered manifest and lock against the reviewed snapshot."""

    problems: list[str] = []
    raw = read(SNAPSHOT_FILE)
    if raw is None:
        return None, [f"{SNAPSHOT_FILE} is missing"]
    snaps: dict[str, Any] = json.loads(raw)
    entries: dict[str, dict[str, Any]] = snaps.get("manifests", {})
    actives = [n for n, e in entries.items() if e.get("status") == "active"]
    if actives != [snaps.get("active")]:
        problems.append(f"exactly one active manifest must match 'active', found {actives}")
    active: Manifest | None = None
    for name, entry in sorted(entries.items()):
        mtext, ltext = read(entry["manifest"]), read(entry["lock"])
        if mtext is None or ltext is None:
            problems.append(f"{name}: manifest or lock file missing")
            continue
        if _sha(mtext) != entry["manifest_sha256"]:
            problems.append(
                f"{entry['manifest']} does not match the reviewed snapshot "
                f"(sha256 {_sha(mtext)[:12]}… != {entry['manifest_sha256'][:12]}…)"
            )
        if _sha(ltext) != entry["lock_sha256"]:
            problems.append(f"{entry['lock']} does not match the reviewed snapshot")
        manifest, parse_problems = parse_manifest(mtext)
        if manifest.name != name:
            problems.append(f"snapshot entry {name} points at manifest {manifest.name}")
        problems += [f"{name}: lock: {p}" for p in verify_lock(manifest, json.loads(ltext))]
        if entry.get("status") == "superseded":
            if not entry.get("superseded_by") or not entry.get("reason"):
                problems.append(f"{name}: a superseded manifest needs superseded_by and reason")
        elif entry.get("status") == "active":
            problems += [f"{name}: {p}" for p in parse_problems]
            problems += [f"{name}: exposure: {p}" for p in audit_exposure(manifest)]
            active = manifest
        else:
            problems.append(f"{name}: unknown status {entry.get('status')!r}")
    return active, problems


# ------------------------------------------------------------------------ base branch
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)  # noqa: S603, S607


def check_against_base(base: str, repo: Path = REPO, m7_rel: str = M7_REL) -> list[str]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", base, "--", m7_rel)
    if listing.returncode != 0:
        return [f"cannot read base {base!r}: {listing.stderr.decode().strip()}"]
    problems: list[str] = []
    frozen = [
        p
        for p in listing.stdout.decode().splitlines()
        if p.rsplit("/", 1)[-1].startswith("split-manifest-")
    ]
    for rel in frozen:
        old = _git(repo, "show", f"{base}:{rel}").stdout
        path = repo / rel
        if not path.is_file() or path.read_bytes() != old:
            problems.append(f"{rel} is frozen on {base} and was changed or removed")
    snap_rel = f"{m7_rel}/{SNAPSHOT_FILE}"
    shown = _git(repo, "show", f"{base}:{snap_rel}")
    if shown.returncode == 0:
        old_snaps = json.loads(shown.stdout)
        path = repo / snap_rel
        new_snaps = json.loads(path.read_text("utf-8")) if path.is_file() else {}
        for name, old in old_snaps.get("manifests", {}).items():
            new = new_snaps.get("manifests", {}).get(name)
            if new is None:
                problems.append(f"snapshot entry {name} on {base} was removed")
                continue
            for field in IMMUTABLE_FIELDS:
                if new.get(field) != old.get(field):
                    problems.append(f"snapshot entry {name}.{field} on {base} was changed")
            if old.get("status") != new.get("status") and not (
                old.get("status") == "active" and new.get("status") == "superseded"
            ):
                problems.append(
                    f"snapshot entry {name}: status {old.get('status')} -> "
                    f"{new.get('status')} is not allowed (only active -> superseded)"
                )
    return problems


# ------------------------------------------------------------------- freeze-then-derive
def check_freeze_order(lock_file: str, index_file: str, repo: Path = REPO) -> list[str]:
    lock_rel, index_rel = f"{M7_REL}/{lock_file}", f"{M7_REL}/{index_file}"
    added = _git(repo, "log", "--diff-filter=A", "--format=%H", "--", lock_rel)
    changed = _git(repo, "log", "-1", "--format=%H", "--", index_rel)
    lock_commits = added.stdout.decode().split()
    index_commit = changed.stdout.decode().strip()
    if added.returncode or not lock_commits:
        return [f"git history: no commit adds {lock_rel} (shallow clone or uncommitted?)"]
    if changed.returncode or not index_commit:
        return [f"git history: no commit changes {index_rel}"]
    freeze_commit = lock_commits[-1]
    if freeze_commit == index_commit:
        return [f"{lock_file} and the pilot were committed together; freeze first"]
    ancestor = _git(repo, "merge-base", "--is-ancestor", freeze_commit, index_commit)
    if ancestor.returncode != 0:
        return [f"freeze commit {freeze_commit[:10]} is not an ancestor of the pilot commit"]
    return []


def snapshot_entry(manifest_file: str, lock_file: str) -> dict[str, Any]:
    """What a reviewer adds to frozen-snapshots.json, printed (never written) by freeze."""

    mtext = (M7 / manifest_file).read_text("utf-8")
    manifest, _ = parse_manifest(mtext)
    return {
        manifest.name: {
            "manifest": manifest_file,
            "lock": lock_file,
            "manifest_sha256": _sha(mtext),
            "lock_sha256": _sha((M7 / lock_file).read_text("utf-8")),
            "frozen_at": manifest.frozen_at,
            "status": "active",
        }
    }
