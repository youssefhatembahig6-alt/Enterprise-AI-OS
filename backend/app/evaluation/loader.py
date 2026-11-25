"""Manifest-driven corpus loader: the only supported way for ML code to read M7 sources.

A loader call names its *purpose*. The purpose fixes the splits it may read:

    training          -> train only
    tuning            -> dev only
    model-selection   -> dev only

No purpose can read held-out material. Held-out scoring is not done from this workspace
(see docs/evaluation/m7-v3-corpus.md, "Blind release data").

Order of operations, so ineligible material is never opened:
  1. Read the reviewed snapshot, the active manifest and its lock: metadata only. Check
     both hashes against the snapshot. The corpus index (corpus-v3/corpus.toml) is NOT
     read, because it carries every document's title, held-out ones included.
  2. Select eligible member references from the manifest's family/split table.
  3. Only then open each eligible document (and, for fresh documents, its facts sidecar),
     at a path built from its key. No directory is listed or globbed, so held-out files
     sitting in the same directory are never touched.
  4. Verify each loaded member against its fingerprint in the frozen lock before
     returning it.

The integrity tooling (``check``, ``overlap``, lock verification) reads every member,
held-out included, to hash and compare it. That is not training or tuning, and it never
returns content to a caller. ML code must not import ``app.evaluation.sources`` or read
``corpus-v3`` directly: the exposure audit fails CI if a development artifact does.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.documents.parsers import ParseFailed, parse

BACKEND = Path(__file__).resolve().parents[2]
M7_DIR = BACKEND / "fixtures" / "evaluation" / "m7"
FRESH_DIR = M7_DIR / "corpus-v3"
DOCS_DIR = BACKEND / "fixtures" / "documents"
LEGAL_DIR = BACKEND / "fixtures" / "legal"
CODE_DIR = BACKEND / "fixtures" / "github-acceptance"


class Purpose(StrEnum):
    TRAINING = "training"
    TUNING = "tuning"
    MODEL_SELECTION = "model-selection"


ALLOWED_SPLITS: dict[Purpose, frozenset[str]] = {
    Purpose.TRAINING: frozenset({"train"}),
    Purpose.TUNING: frozenset({"dev"}),
    Purpose.MODEL_SELECTION: frozenset({"dev"}),
}


class LoaderError(RuntimeError):
    """The frozen metadata or a selected file does not match the reviewed freeze."""


@dataclass(frozen=True)
class Eligible:
    ref: str
    family: str
    split: str


@dataclass(frozen=True)
class LoadedDocument:
    ref: str
    family: str
    split: str
    text: str
    facts: dict[str, Any] | None


@dataclass
class LoadResult:
    purpose: Purpose
    manifest: str
    documents: list[LoadedDocument] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # ref -> reason
    manifest_sha256: str = ""
    lock_sha256: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Pin:
    """A specific frozen manifest, by name and the reviewed SHA-256 of manifest and lock.

    Pinned data (e.g. a released training batch) is built and verified against exactly
    this snapshot, whichever manifest is active later. A superseded manifest stays usable
    because superseded manifests and their locks are immutable on ``main``.
    """

    name: str
    manifest_sha256: str
    lock_sha256: str


def _frozen_metadata(
    m7_dir: Path, pin: Pin | None = None
) -> tuple[str, dict[str, Any], dict[str, str], dict[str, Any]]:
    """(name, parsed manifest, lock member fingerprints, snapshot entry), hash-checked.

    Without ``pin`` the active manifest is used; with it, exactly the pinned one.
    """

    snaps = json.loads((m7_dir / "frozen-snapshots.json").read_text("utf-8"))
    name = pin.name if pin else snaps["active"]
    if name not in snaps["manifests"]:
        raise LoaderError(f"{name} is not in the reviewed snapshot")
    entry = snaps["manifests"][name]
    if pin and (entry["manifest_sha256"], entry["lock_sha256"]) != (
        pin.manifest_sha256,
        pin.lock_sha256,
    ):
        raise LoaderError(f"the reviewed snapshot's entry for {name} differs from the pin")
    manifest_bytes = (m7_dir / entry["manifest"]).read_bytes()
    lock_bytes = (m7_dir / entry["lock"]).read_bytes()
    if _sha(manifest_bytes) != entry["manifest_sha256"]:
        raise LoaderError(f"{entry['manifest']} does not match the reviewed snapshot")
    if _sha(lock_bytes) != entry["lock_sha256"]:
        raise LoaderError(f"{entry['lock']} does not match the reviewed snapshot")
    manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
    lock = json.loads(lock_bytes)
    if lock.get("manifest_sha256") != entry["manifest_sha256"]:
        raise LoaderError(f"the lock does not belong to {name}")
    if manifest.get("name") != name:
        raise LoaderError(f"{entry['manifest']} is named {manifest.get('name')}, not {name}")
    return name, manifest, lock["member_sha256"], entry


def eligible(
    purpose: Purpose | str, m7_dir: Path = M7_DIR, pin: Pin | None = None
) -> list[Eligible]:
    """Members a purpose may load, chosen from frozen metadata alone (no source opened)."""

    purpose = Purpose(purpose)
    allowed = ALLOWED_SPLITS[purpose]
    if "heldout" in allowed:  # pragma: no cover - a guard against a future edit
        raise LoaderError("no purpose may load held-out material")
    _, manifest, _, _ = _frozen_metadata(m7_dir, pin)
    out = [
        Eligible(ref, fam["id"], fam["split"])
        for fam in manifest["families"]
        if fam["split"] in allowed
        for ref in fam["members"]
    ]
    return sorted(out, key=lambda e: e.ref)


def _toml_table(path: Path, table: str) -> dict[str, str]:
    data = tomllib.loads(path.read_text("utf-8"))
    return {row["key"]: row["file"] for row in data[table]}


def load(
    purpose: Purpose | str,
    *,
    m7_dir: Path = M7_DIR,
    fresh_files_dir: Path | None = None,
    fresh_facts_dir: Path | None = None,
    pin: Pin | None = None,
) -> LoadResult:
    """Load every eligible member for ``purpose``, verified against the frozen lock.

    ``fresh_files_dir`` / ``fresh_facts_dir`` override where fresh documents and
    sidecars live (both may be one shared directory). Only ``<key>.md`` and
    ``<key>.json`` of eligible keys are ever opened there. ``pin`` selects a specific
    frozen manifest instead of the active one.
    """

    purpose = Purpose(purpose)
    name, _, fingerprints, entry = _frozen_metadata(m7_dir, pin)
    selected = eligible(purpose, m7_dir, pin)
    files_dir = fresh_files_dir or FRESH_DIR / "files"
    facts_dir = fresh_facts_dir or FRESH_DIR / "facts"
    result = LoadResult(
        purpose=purpose,
        manifest=name,
        manifest_sha256=entry["manifest_sha256"],
        lock_sha256=entry["lock_sha256"],
    )
    tables: dict[str, dict[str, str]] = {}
    for item in selected:
        kind, _, key = item.ref.partition(":")
        if kind == "sql":
            result.skipped[item.ref] = "SQL template: no document text"
            continue
        facts: dict[str, Any] | None = None
        if kind == "fresh":
            doc_bytes = (files_dir / f"{key}.md").read_bytes()
            facts_bytes = (facts_dir / f"{key}.json").read_bytes()
            both = {"file": _sha(doc_bytes), "facts": _sha(facts_bytes)}
            digest = _sha(json.dumps(both, sort_keys=True).encode())
            facts = json.loads(facts_bytes)
            filename = f"{key}.md"
        else:
            if kind == "doc":
                tables.setdefault("doc", _toml_table(DOCS_DIR / "corpus.toml", "documents"))
                path = DOCS_DIR / "files" / tables["doc"][key]
            elif kind == "contract":
                tables.setdefault("contract", _toml_table(LEGAL_DIR / "legal.toml", "contracts"))
                path = LEGAL_DIR / "files" / tables["contract"][key]
            elif kind == "code":
                path = CODE_DIR / key
            else:
                raise LoaderError(f"unknown member kind in {item.ref}")
            doc_bytes = path.read_bytes()
            digest = _sha(doc_bytes)
            filename = path.name
        if digest != fingerprints.get(item.ref):
            raise LoaderError(f"{item.ref} does not match its fingerprint in the frozen lock")
        if kind == "code":
            text = doc_bytes.decode("utf-8")
        else:
            try:
                text = parse(filename, doc_bytes).text
            except ParseFailed as exc:
                result.skipped[item.ref] = f"parser: {exc.code}"
                continue
        result.documents.append(LoadedDocument(item.ref, item.family, item.split, text, facts))
    return result
