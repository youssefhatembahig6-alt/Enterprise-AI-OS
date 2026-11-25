"""The frozen split manifest: structure, freeze lock and split/family lookups."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.evaluation.sources import (
    BACKEND,
    M7,
    SourceError,
    all_file_refs,
    sha256_file,
    source_path,
    split_ref,
)

MANIFEST = M7 / "split-manifest-v1.toml"
LOCK = M7 / "split-manifest-v1.lock.json"


@dataclass(frozen=True)
class Manifest:
    version: int
    name: str
    frozen_at: str
    splits: tuple[str, ...]
    family_split: dict[str, str]
    member_family: dict[str, str]
    sql_templates: dict[str, dict[str, Any]]
    legacy_fixtures: tuple[dict[str, str], ...]
    sha256: str

    def split_of_ref(self, ref: str) -> str:
        return self.family_split[self.member_family[ref]]

    def sealed(self) -> set[str]:
        return {f for f, s in self.family_split.items() if s == "heldout"}


def parse_manifest(text: str) -> tuple[Manifest, list[str]]:
    """Parse and validate. Returns the manifest and a list of problems (empty = valid)."""

    data = tomllib.loads(text)
    problems: list[str] = []
    splits = tuple(data.get("splits", ()))
    if set(splits) != {"train", "dev", "heldout"}:
        problems.append(f"splits must be train/dev/heldout, got {splits}")
    family_split: dict[str, str] = {}
    member_family: dict[str, str] = {}
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
        sql_templates=templates,
        legacy_fixtures=tuple(data.get("legacy_fixtures", [])),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    return manifest, problems


def load_manifest(path: Path = MANIFEST) -> tuple[Manifest, list[str]]:
    return parse_manifest(path.read_text("utf-8"))


def member_fingerprints(manifest: Manifest) -> dict[str, str]:
    out: dict[str, str] = {}
    for ref in sorted(manifest.member_family):
        path = source_path(ref)
        if path is not None:
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


def build_lock(manifest: Manifest) -> dict[str, Any]:
    return {
        "manifest": MANIFEST.name,
        "name": manifest.name,
        "version": manifest.version,
        "frozen_at": manifest.frozen_at,
        "manifest_sha256": manifest.sha256,
        "member_sha256": member_fingerprints(manifest),
    }


def verify_lock(manifest: Manifest, lock: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if lock.get("manifest_sha256") != manifest.sha256:
        problems.append(
            "split manifest changed after it was frozen (SHA-256 differs from the lock); "
            "create split-manifest-v2 instead of editing v1"
        )
    current = member_fingerprints(manifest)
    frozen = lock.get("member_sha256", {})
    for ref in sorted(set(current) | set(frozen)):
        if current.get(ref) != frozen.get(ref):
            problems.append(f"source {ref} changed after the split was frozen")
    return problems


def load_lock(path: Path = LOCK) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return result
