"""Verified access to the NileTech 0.1.6 business-data baseline.

Two source forms are accepted, and both are verified before any record is read:

* the original export ZIP (``niletech-0.1.6.zip``): every entry listed in its
  embedded ``manifest.json`` must be present with the recorded SHA-256, no
  unlisted entries may exist, every table must have the recorded row count, and
  the manifest's ``content_fingerprint`` must recompute;
* the vendored subset in ``backend/fixtures/business/niletech-0.1.6``: the
  untouched source ``manifest.json`` (pinned by its own SHA-256) plus the sales
  tables as deterministic gzip files whose *decompressed* bytes must match the
  SHA-256 recorded in that manifest.

The pinned provenance (generator 0.1.6, commit bf4a247…, seed 20260630,
reference date 2026-06-30) must match the manifest exactly.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASET = "niletech-0.1.6"
EXPECTED_PROVENANCE: dict[str, Any] = {
    "tenant": "niletech",
    "generator_version": "0.1.6",
    "source_revision": "bf4a247a5c3db8bcedc37e0ed47dad6c635d9349",
    "seed": 20260630,
    "reference_date": "2026-06-30",
}
EXPECTED_CONTENT_FINGERPRINT = "36c7784f6754f44a17d08c96018f3bbea1512490079167a33df55a1f311122f0"
# SHA-256 of the embedded manifest.json bytes, as shipped in the export ZIP.
EXPECTED_MANIFEST_SHA256 = "885acf8aa2ccf5006a7e168cc8a52f4cbd81e3637052beb5cd815151c79ad99f"
# Key counts the assignment requires before loading anything.
EXPECTED_KEY_COUNTS = {
    "companies": 1,
    "users": 200,
    "documents": 70,
    "orders": 2400,
    "invoices": 2400,
}
EXPECTED_DOCUMENT_FILES = 70

# Tables imported in M1, in foreign-key order.
SALES_TABLES = ("companies", "customers", "products", "orders", "order_lines", "invoices")

VENDORED_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "business" / DATASET


class SourceVerificationError(ValueError):
    """The source does not match its manifest or the pinned provenance."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_fingerprint(file_sha256: dict[str, str]) -> str:
    """The generator's fingerprint: SHA-256 of the compact, key-sorted hash map."""

    return sha256_hex(json.dumps(file_sha256, sort_keys=True, separators=(",", ":")).encode())


def _count_rows(raw: bytes) -> int:
    return sum(1 for line in raw.splitlines() if line.strip())


@dataclass(frozen=True)
class VerifiedSource:
    """Raw table bytes that passed verification, plus the parsed manifest."""

    manifest: dict[str, Any]
    manifest_sha256: str
    tables: dict[str, bytes]
    form: str  # "zip" | "vendored"

    def records(self, table: str) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.tables[table].splitlines() if line.strip()]


def _check_manifest(manifest: dict[str, Any], manifest_bytes: bytes) -> str:
    for key, expected in EXPECTED_PROVENANCE.items():
        if manifest.get(key) != expected:
            raise SourceVerificationError(
                f"manifest {key}={manifest.get(key)!r}, expected {expected!r}"
            )
    file_sha256 = manifest.get("file_sha256")
    if not isinstance(file_sha256, dict) or not file_sha256:
        raise SourceVerificationError("manifest has no file_sha256 map")
    recomputed = content_fingerprint(file_sha256)
    if manifest.get("content_fingerprint") != recomputed:
        raise SourceVerificationError("manifest content_fingerprint does not recompute")
    if recomputed != EXPECTED_CONTENT_FINGERPRINT:
        raise SourceVerificationError("content fingerprint differs from the pinned baseline")
    row_counts = manifest.get("row_counts", {})
    for table, count in EXPECTED_KEY_COUNTS.items():
        if row_counts.get(table) != count:
            raise SourceVerificationError(
                f"key count {table}={row_counts.get(table)}, expected {count}"
            )
    documents = [p for p in file_sha256 if p.startswith("documents/")]
    if len(documents) != EXPECTED_DOCUMENT_FILES:
        raise SourceVerificationError(f"{len(documents)} document files, expected 70")
    digest = sha256_hex(manifest_bytes)
    if digest != EXPECTED_MANIFEST_SHA256:
        raise SourceVerificationError("manifest.json bytes differ from the pinned baseline")
    return digest


def _check_table(manifest: dict[str, Any], table: str, raw: bytes) -> None:
    path = f"tables/{table}.jsonl"
    if sha256_hex(raw) != manifest["file_sha256"].get(path):
        raise SourceVerificationError(f"{path}: SHA-256 does not match the manifest")
    if _count_rows(raw) != manifest["row_counts"].get(table):
        raise SourceVerificationError(f"{path}: row count does not match the manifest")


def verify_zip(path: Path, tables: Iterable[str] = SALES_TABLES) -> VerifiedSource:
    """Verify the whole export ZIP, then return the requested tables."""

    with zipfile.ZipFile(path) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        for name in names:
            if name.startswith("/") or ".." in name.split("/"):
                raise SourceVerificationError(f"unsafe path in archive: {name!r}")
        if "manifest.json" not in names:
            raise SourceVerificationError("archive has no manifest.json")
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        digest = _check_manifest(manifest, manifest_bytes)
        listed = set(manifest["file_sha256"])
        present = set(names) - {"manifest.json"}
        if present != listed:
            raise SourceVerificationError(
                f"archive/manifest mismatch: unlisted={sorted(present - listed)[:5]}"
                f" missing={sorted(listed - present)[:5]}"
            )
        contents = {name: archive.read(name) for name in sorted(listed)}
    for name, data in contents.items():
        if sha256_hex(data) != manifest["file_sha256"][name]:
            raise SourceVerificationError(f"{name}: SHA-256 does not match the manifest")
    for table, count in manifest["row_counts"].items():
        if _count_rows(contents[f"tables/{table}.jsonl"]) != count:
            raise SourceVerificationError(f"tables/{table}.jsonl: row count mismatch")
    selected = {t: contents[f"tables/{t}.jsonl"] for t in tables}
    return VerifiedSource(manifest=manifest, manifest_sha256=digest, tables=selected, form="zip")


def verify_vendored(directory: Path = VENDORED_DIR) -> VerifiedSource:
    """Verify the vendored subset against the untouched source manifest."""

    manifest_bytes = (directory / "source-manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    digest = _check_manifest(manifest, manifest_bytes)
    tables: dict[str, bytes] = {}
    for table in SALES_TABLES:
        raw = gzip.decompress((directory / "tables" / f"{table}.jsonl.gz").read_bytes())
        _check_table(manifest, table, raw)
        tables[table] = raw
    return VerifiedSource(manifest=manifest, manifest_sha256=digest, tables=tables, form="vendored")


def deterministic_gzip(data: bytes) -> bytes:
    """Gzip with no file name and mtime 0, so identical input gives identical bytes."""

    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as gz:
        gz.write(data)
    return buffer.getvalue()


def vendor_subset(zip_path: Path, out_dir: Path) -> dict[str, Any]:
    """Write the verified sales subset of the export to ``out_dir``; return its manifest."""

    with zipfile.ZipFile(zip_path) as archive:
        manifest_bytes = archive.read("manifest.json")
    source = verify_zip(zip_path)
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)
    (out_dir / "source-manifest.json").write_bytes(manifest_bytes)
    files: dict[str, Any] = {}
    for table in SALES_TABLES:
        packed = deterministic_gzip(source.tables[table])
        (out_dir / "tables" / f"{table}.jsonl.gz").write_bytes(packed)
        files[f"tables/{table}.jsonl.gz"] = {
            "source_path": f"tables/{table}.jsonl",
            "source_sha256": source.manifest["file_sha256"][f"tables/{table}.jsonl"],
            "gzip_sha256": sha256_hex(packed),
            "rows": source.manifest["row_counts"][table],
        }
    return {
        "dataset": DATASET,
        **EXPECTED_PROVENANCE,
        "source_archive": "niletech-0.1.6.zip",
        "source_archive_sha256": sha256_hex(zip_path.read_bytes()),
        "source_manifest_sha256": sha256_hex(manifest_bytes),
        "source_content_fingerprint": source.manifest["content_fingerprint"],
        "source_key_counts": {k: source.manifest["row_counts"][k] for k in EXPECTED_KEY_COUNTS},
        "source_document_files": EXPECTED_DOCUMENT_FILES,
        "vendored_tables": files,
    }
