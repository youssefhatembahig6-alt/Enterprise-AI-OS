"""Source verification and reproducibility of the NileTech 0.1.6 baseline (no database)."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.business_data.model import dataset_fingerprint, normalize
from app.business_data.source import (
    EXPECTED_CONTENT_FINGERPRINT,
    SALES_TABLES,
    VENDORED_DIR,
    SourceVerificationError,
    content_fingerprint,
    vendor_subset,
    verify_vendored,
    verify_zip,
)

BACKEND = Path(__file__).resolve().parents[2]
PINNED_NORMALIZED_FINGERPRINT = "e5715a94f93ed42dd31b3f00337af4bb591495a0cfd96f7a5a8ca90555bef127"
# Default: <Grad>/reference-data next to the repository checkout (absent in CI/containers).
REFERENCE_ZIP = Path(
    os.environ.get(
        "EAIOS_REFERENCE_ZIP", BACKEND.parent.parent / "reference-data" / "niletech-0.1.6.zip"
    )
)
needs_zip = pytest.mark.skipif(
    not REFERENCE_ZIP.exists(), reason=f"original export not available at {REFERENCE_ZIP}"
)


def test_vendored_subset_verifies_against_source_manifest() -> None:
    source = verify_vendored()
    manifest = source.manifest
    assert manifest["source_revision"] == "bf4a247a5c3db8bcedc37e0ed47dad6c635d9349"
    assert (manifest["seed"], manifest["reference_date"]) == (20260630, "2026-06-30")
    assert manifest["content_fingerprint"] == EXPECTED_CONTENT_FINGERPRINT
    counts = manifest["row_counts"]
    assert (counts["companies"], counts["users"], counts["orders"], counts["invoices"]) == (
        1,
        200,
        2400,
        2400,
    )
    assert sum(1 for p in manifest["file_sha256"] if p.startswith("documents/")) == 70
    assert set(source.tables) == set(SALES_TABLES)


def test_vendor_manifest_records_provenance_and_hashes() -> None:
    vendor = json.loads((VENDORED_DIR / "vendor-manifest.json").read_text())
    source = json.loads((VENDORED_DIR / "source-manifest.json").read_bytes())
    assert vendor["source_revision"] == source["source_revision"]
    assert vendor["normalized_fingerprint"] == PINNED_NORMALIZED_FINGERPRINT
    for path, entry in vendor["vendored_tables"].items():
        assert entry["source_sha256"] == source["file_sha256"][entry["source_path"]]
        assert entry["rows"] == source["row_counts"][Path(entry["source_path"]).stem]
        import hashlib

        assert (
            hashlib.sha256((VENDORED_DIR / path).read_bytes()).hexdigest() == entry["gzip_sha256"]
        )


def _copy_vendored(tmp_path: Path) -> Path:
    target = tmp_path / "vendored"
    shutil.copytree(VENDORED_DIR, target)
    return target


def test_tampered_table_is_rejected(tmp_path: Path) -> None:
    target = _copy_vendored(tmp_path)
    path = target / "tables" / "orders.jsonl.gz"
    raw = gzip.decompress(path.read_bytes()).replace(b'"FULFILLED"', b'"CANCELLED"', 1)
    path.write_bytes(gzip.compress(raw, mtime=0))
    with pytest.raises(SourceVerificationError, match=r"orders\.jsonl: SHA-256"):
        verify_vendored(target)


def test_dropped_row_is_rejected(tmp_path: Path) -> None:
    target = _copy_vendored(tmp_path)
    path = target / "tables" / "invoices.jsonl.gz"
    lines = gzip.decompress(path.read_bytes()).splitlines(keepends=True)
    path.write_bytes(gzip.compress(b"".join(lines[:-1]), mtime=0))
    with pytest.raises(SourceVerificationError):
        verify_vendored(target)


@pytest.mark.parametrize(
    ("field", "value"),
    [("seed", 1), ("source_revision", "0" * 40), ("reference_date", "2026-07-01")],
)
def test_wrong_provenance_is_rejected(tmp_path: Path, field: str, value: object) -> None:
    target = _copy_vendored(tmp_path)
    manifest = json.loads((target / "source-manifest.json").read_bytes())
    manifest[field] = value
    (target / "source-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(SourceVerificationError):
        verify_vendored(target)


def test_content_fingerprint_definition() -> None:
    manifest = json.loads((VENDORED_DIR / "source-manifest.json").read_bytes())
    assert content_fingerprint(manifest["file_sha256"]) == manifest["content_fingerprint"]


def test_normalized_fingerprint_is_pinned() -> None:
    assert dataset_fingerprint(normalize(verify_vendored())) == PINNED_NORMALIZED_FINGERPRINT


def test_two_clean_generations_have_identical_fingerprints() -> None:
    """Fresh interpreters with different hash seeds produce the same fingerprint."""

    script = (
        "from app.business_data.source import verify_vendored;"
        "from app.business_data.model import normalize, dataset_fingerprint;"
        "print(dataset_fingerprint(normalize(verify_vendored())))"
    )
    outputs = []
    for hash_seed in ("1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(BACKEND)}
        result = subprocess.run(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", script],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout.strip())
    assert outputs == [PINNED_NORMALIZED_FINGERPRINT] * 2


@needs_zip
def test_original_zip_verifies_fully() -> None:
    source = verify_zip(REFERENCE_ZIP)
    assert source.form == "zip"
    assert dataset_fingerprint(normalize(source)) == PINNED_NORMALIZED_FINGERPRINT


@needs_zip
def test_vendoring_twice_is_byte_identical_to_committed_fixture(tmp_path: Path) -> None:
    for run in ("a", "b"):
        vendor_subset(REFERENCE_ZIP, tmp_path / run)
    for relative in ["source-manifest.json", *[f"tables/{t}.jsonl.gz" for t in SALES_TABLES]]:
        first = (tmp_path / "a" / relative).read_bytes()
        assert first == (tmp_path / "b" / relative).read_bytes()
        assert first == (VENDORED_DIR / relative).read_bytes(), f"{relative} drifted"


@needs_zip
def test_zip_with_unlisted_entry_is_rejected(tmp_path: Path) -> None:
    copy = tmp_path / "extra.zip"
    shutil.copy(REFERENCE_ZIP, copy)
    with zipfile.ZipFile(copy, "a") as archive:
        archive.writestr("tables/smuggled.jsonl", b"{}\n")
    with pytest.raises(SourceVerificationError, match="unlisted"):
        verify_zip(copy)
