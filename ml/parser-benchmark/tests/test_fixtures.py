"""Fixture integrity: hashes, provenance labels, gold consistency and regeneration."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"
MANIFEST = json.loads((FIX / "manifest.json").read_text(encoding="utf-8"))
FONTS = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Manifest(unittest.TestCase):
    def test_hashes_and_sizes(self) -> None:
        for e in MANIFEST["fixtures"]:
            with self.subTest(e["id"]):
                f = FIX / e["file"]
                self.assertEqual(sha(f), e["sha256"])
                self.assertEqual(f.stat().st_size, e["bytes"])
                self.assertEqual(sha(FIX / e["gold"]), e["gold_sha256"])
                g = json.loads((FIX / e["gold"]).read_text(encoding="utf-8"))
                self.assertEqual(g["file_sha256"], e["sha256"])

    def test_labelled_synthetic_with_known_sources(self) -> None:
        known = set(MANIFEST["provenance"]) - {"excluded"}
        for e in MANIFEST["fixtures"]:
            self.assertEqual(e["label"], "SYNTHETIC")
            self.assertTrue(set(e["sources"]) <= known, e["sources"])

    def test_cases_cover_the_roadmap(self) -> None:
        cases = {e["case"] for e in MANIFEST["fixtures"]}
        self.assertEqual(cases, {"digital_pdf", "two_column_pdf", "table_heavy_pdf", "scanned_pdf", "csv", "xlsx"})

    def test_public_baseline_hash_matches_vendored_manifest(self) -> None:
        src = json.loads(
            (ROOT.parents[1] / "backend/fixtures/business/niletech-0.1.6/source-manifest.json").read_text()
        )
        pub = MANIFEST["provenance"]["baseline-public"]
        self.assertEqual(src["file_sha256"][pub["path"]], pub["sha256"])
        self.assertEqual(pub["classification"], "PUBLIC")


class GoldConsistency(unittest.TestCase):
    def test_anchors_appear_in_order_in_gold_text(self) -> None:
        from bench.scoring import find_anchor, norm_text

        for e in MANIFEST["fixtures"]:
            if not e["case"].endswith("_pdf"):
                continue
            g = json.loads((FIX / e["gold"]).read_text(encoding="utf-8"))
            t = norm_text(g["text"])
            pos = [find_anchor(t, a) for a in g["reading_order"]]
            with self.subTest(e["id"]):
                self.assertNotIn(-1, pos)

    def test_table_heavy_matches_frozen_sales_figures(self) -> None:
        g = json.loads((FIX / "gold/table-heavy-sales-q1-2026.json").read_text(encoding="utf-8"))
        t1 = next(t for t in g["tables"] if t["id"] == "T1")
        self.assertEqual(t1["rows"][-1], ["Total", "291", "80,934,385.00", "11,330,813.90", "92,265,198.90"])
        t2 = next(t for t in g["tables"] if t["id"] == "T2")
        self.assertEqual(t2["rows"][-1], ["All statuses", "359", "102,987,786.00"])

    def test_scanned_fixture_has_no_text_layer(self) -> None:
        import pymupdf

        with pymupdf.open(FIX / "files/scanned-delivery-note.pdf") as doc:
            self.assertEqual(sum(len(p.get_text().strip()) for p in doc), 0)
            self.assertEqual(len(doc[0].get_images()), 1)


@unittest.skipUnless(all(f.exists() for f in FONTS), "DejaVu fonts not installed")
class Regeneration(unittest.TestCase):
    def test_generator_reproduces_committed_bytes(self) -> None:
        r = subprocess.run([sys.executable, str(FIX / "generate.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
