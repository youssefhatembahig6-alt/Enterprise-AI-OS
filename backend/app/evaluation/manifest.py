"""Versioned, frozen split manifests: structure, locks, exposure audit, split lookups.

The active version is named by ``frozen-snapshots.json`` (the independently reviewed
snapshot, see ``app.evaluation.freeze``). Superseded versions stay on disk unchanged as
audit records and must still verify against their own locks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from app.evaluation.sources import (
    BACKEND,
    M7,
    SourceError,
    all_file_refs,
    fresh_facts_path,
    sha256_file,
    source_path,
    split_ref,
)

REPO = BACKEND.parent
SNAPSHOTS = M7 / "frozen-snapshots.json"
# Development artifacts scanned for exposure (repository-relative). M7's own files are
# excluded: they cite sources by design.
EXPOSURE_ROOTS = (
    "backend/tests",
    "backend/app",
    "backend/fixtures/evaluation",
    "scripts",
    "ml",
    "docs/evidence",
)
EXPOSURE_EXCLUDE = (
    "backend/app/evaluation/",
    "backend/tests/unit/test_m7_evaluation.py",
    "backend/fixtures/evaluation/m7/",
)
# Direct readers. A development artifact that reads M7 sources other than through
# app.evaluation.loader could bulk-load held-out documents without naming any of them.
_DIRECT_READERS = (
    re.compile(r"corpus[-_]v3"),
    re.compile(r"evaluation/m7\b"),
    re.compile(r"\bimport\s+app\.evaluation\.(?!loader\b)\w+"),
    re.compile(r"\bfrom\s+app\.evaluation\.(?!loader\b)\w+\s+import\b"),
)
_FROM_PACKAGE = re.compile(r"\bfrom\s+app\.evaluation\s+import\s+(\([^)]*\)|[^\n]+)")


def _package_import_bypasses(text: str) -> str | None:
    """`from app.evaluation import X, Y`: any name other than `loader` bypasses it."""

    for match in _FROM_PACKAGE.finditer(text):
        names = match.group(1).strip("()").replace("\\", " ").split(",")
        if any(n.split()[0] != "loader" for n in names if n.strip()):
            return match.group(0)
    return None


_SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


@dataclass(frozen=True)
class Manifest:
    version: int
    name: str
    frozen_at: str
    splits: tuple[str, ...]
    family_split: dict[str, str]
    member_family: dict[str, str]
    exposure: dict[str, tuple[str, ...]]
    reserved: dict[str, str]
    sql_templates: dict[str, dict[str, Any]]
    legacy_fixtures: tuple[dict[str, str], ...]
    sha256: str

    def split_of_ref(self, ref: str) -> str:
        return self.family_split[self.member_family[ref]]

    def sealed(self) -> set[str]:
        held = {f for f, s in self.family_split.items() if s == "heldout"}
        return held | {f for f, s in self.reserved.items() if s == "heldout"}


def parse_manifest(text: str) -> tuple[Manifest, list[str]]:
    """Parse and validate. Returns the manifest and a list of problems (empty = valid)."""

    data = tomllib.loads(text)
    problems: list[str] = []
    splits = tuple(data.get("splits", ()))
    if set(splits) != {"train", "dev", "heldout"}:
        problems.append(f"splits must be train/dev/heldout, got {splits}")
    family_split: dict[str, str] = {}
    member_family: dict[str, str] = {}
    exposure: dict[str, tuple[str, ...]] = {}
    templates: dict[str, dict[str, Any]] = data.get("sql_templates", {})
    for fam in data.get("families", []):
        fid = fam["id"]
        if fid in family_split:
            problems.append(f"family {fid} declared twice")
        if fam["split"] not in splits:
            problems.append(f"family {fid} has unknown split {fam['split']!r}")
        if not fam.get("reason"):
            problems.append(f"family {fid} has no recorded assignment reason")
        family_split[fid] = fam["split"]
        exposure[fid] = tuple(fam.get("exposure", ()))
        for ref in fam["members"]:
            try:
                kind, name = split_ref(ref)
                if kind == "sql":
                    if name not in templates:
                        raise SourceError(f"unknown SQL template {name!r}")
                else:
                    source_path(ref)
            except SourceError as exc:
                problems.append(f"family {fid}: {exc}")
                continue
            if ref in member_family:
                problems.append(
                    f"{ref} is in two families ({member_family[ref]}, {fid}): a source "
                    "may belong to exactly one family"
                )
            member_family[ref] = fid
    reserved: dict[str, str] = {}
    for fam in data.get("reserved_families", []):
        if fam["id"] in family_split or fam["id"] in reserved:
            problems.append(f"reserved family {fam['id']} is declared twice")
        if fam["split"] not in splits:
            problems.append(f"reserved family {fam['id']} has unknown split {fam['split']!r}")
        if fam.get("members"):
            problems.append(f"reserved family {fam['id']} must not have members")
        reserved[fam["id"]] = fam["split"]
    for ref in all_file_refs():
        if ref not in member_family:
            problems.append(f"{ref} is not assigned to any family")
    for name in templates:
        if f"sql:{name}" not in member_family:
            problems.append(f"sql:{name} is not assigned to any family")
    manifest = Manifest(
        version=int(data["version"]),
        name=data["name"],
        frozen_at=data["frozen_at"],
        splits=splits,
        family_split=family_split,
        member_family=member_family,
        exposure=exposure,
        reserved=reserved,
        sql_templates=templates,
        legacy_fixtures=tuple(data.get("legacy_fixtures", [])),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    return manifest, problems


# ----------------------------------------------------------------------- snapshot registry
def load_snapshots(text: str | None = None) -> dict[str, Any]:
    raw = SNAPSHOTS.read_text("utf-8") if text is None else text
    result: dict[str, Any] = json.loads(raw)
    return result


def active_entry(snapshots: dict[str, Any] | None = None) -> dict[str, Any]:
    snaps = snapshots if snapshots is not None else load_snapshots()
    entry: dict[str, Any] = snaps["manifests"][snaps["active"]]
    return entry


def active_paths() -> tuple[Path, Path]:
    entry = active_entry()
    return M7 / entry["manifest"], M7 / entry["lock"]


def load_manifest(path: Path | None = None) -> tuple[Manifest, list[str]]:
    return parse_manifest((path or active_paths()[0]).read_text("utf-8"))


def load_lock(path: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((path or active_paths()[1]).read_text("utf-8"))
    return result


# ------------------------------------------------------------------------------ locks
def member_fingerprints(manifest: Manifest) -> dict[str, str]:
    out: dict[str, str] = {}
    for ref in sorted(manifest.member_family):
        path = source_path(ref)
        if path is not None and ref.startswith("fresh:"):
            # A fresh document and its facts sidecar (the later claims' ground truth).
            both = {"file": sha256_file(path), "facts": sha256_file(fresh_facts_path(ref))}
            out[ref] = hashlib.sha256(json.dumps(both, sort_keys=True).encode()).hexdigest()
        elif path is not None:
            out[ref] = sha256_file(path)
        else:
            tpl = manifest.sql_templates[split_ref(ref)[1]]
            out[ref] = hashlib.sha256(
                json.dumps(
                    {
                        "normalized_fingerprint": tpl["normalized_fingerprint"],
                        "fixtures": {p: sha256_file(BACKEND / p) for p in tpl["fixtures"]},
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
    return out


def build_lock(manifest: Manifest, manifest_file: str) -> dict[str, Any]:
    return {
        "manifest": manifest_file,
        "name": manifest.name,
        "version": manifest.version,
        "frozen_at": manifest.frozen_at,
        "manifest_sha256": manifest.sha256,
        "member_sha256": member_fingerprints(manifest),
    }


def lock_text(lock: dict[str, Any]) -> str:
    return json.dumps(lock, indent=2, sort_keys=True) + "\n"


def verify_lock(manifest: Manifest, lock: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if lock.get("manifest_sha256") != manifest.sha256:
        problems.append(
            f"{manifest.name} changed after it was frozen (SHA-256 differs from its lock); "
            "create a new manifest version instead of editing a frozen one"
        )
    current = member_fingerprints(manifest)
    frozen = lock.get("member_sha256", {})
    for ref in sorted(set(current) | set(frozen)):
        if current.get(ref) != frozen.get(ref):
            problems.append(f"source {ref} changed after {manifest.name} was frozen")
    return problems


# ------------------------------------------------------------------------ exposure audit
def member_tokens(manifest: Manifest, ref: str) -> tuple[str, ...]:
    """Strings whose presence in a development artifact means the member was used."""

    kind, name = split_ref(ref)
    path = source_path(ref)
    if path is None:  # SQL template: its name and the fixture files that define it
        fixtures = manifest.sql_templates[name]["fixtures"]
        return (name, *(Path(f).name for f in fixtures))
    if kind == "code":
        return (name, path.name)
    return (name, path.name, path.stem)


@cache
def _artifact_texts() -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for root in EXPOSURE_ROOTS:
        for path in _walk(REPO / root):
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(EXPOSURE_EXCLUDE) or rel in EXPOSURE_EXCLUDE:
                continue
            out.append((rel, path.read_bytes().decode("utf-8", errors="replace")))
    return tuple(out)


def _walk(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan_exposure(manifest: Manifest) -> dict[str, set[str]]:
    """family -> development artifacts that mention any of its members."""

    found: dict[str, set[str]] = {f: set() for f in manifest.family_split}
    tokens = {ref: member_tokens(manifest, ref) for ref in manifest.member_family}
    for rel, text in _artifact_texts():
        for ref, toks in tokens.items():
            if any(t in text for t in toks):
                found[manifest.member_family[ref]].add(rel)
    return found


def direct_readers() -> list[tuple[str, str]]:
    """(artifact, matched text) for development artifacts that bypass the loader."""

    hits: list[tuple[str, str]] = []
    for rel, text in _artifact_texts():
        found = next((m.group(0) for p in _DIRECT_READERS if (m := p.search(text))), None)
        found = found or _package_import_bypasses(text)
        if found:
            hits.append((rel, found.strip()))
    return hits


def audit_exposure(manifest: Manifest) -> list[str]:
    problems: list[str] = [
        f"{rel} reads M7 sources directly ({what!r}); use app.evaluation.loader, which "
        "selects train or dev documents before opening any file"
        for rel, what in direct_readers()
    ]
    scanned = scan_exposure(manifest)
    for fid, split in sorted(manifest.family_split.items()):
        members = [r for r, f in manifest.member_family.items() if f == fid]
        toks = [t for r in members for t in member_tokens(manifest, r)]
        for rel in manifest.exposure.get(fid, ()):
            path = REPO / rel
            if not path.is_file():
                problems.append(f"family {fid}: listed exposure {rel} does not exist")
            elif not any(t in path.read_text("utf-8", errors="replace") for t in toks):
                problems.append(f"family {fid}: listed exposure {rel} mentions no member")
        if split == "dev" and not manifest.exposure.get(fid):
            problems.append(f"family {fid} is dev but lists no exposure evidence")
        if split != "dev" and scanned[fid]:
            sample = ", ".join(sorted(scanned[fid])[:3])
            problems.append(
                f"family {fid} is {split} but development artifacts mention it ({sample}): "
                "a development-exposed family must be dev"
            )
    return problems
