"""bench.score_app: application exports are matched to M1 gold by file hash and scored
with the unchanged M1 scorer."""

import json
import unittest
from pathlib import Path

from bench.score_app import markdown, score_export

ROOT = Path(__file__).resolve().parents[1]


class ScoreAppTest(unittest.TestCase):
    def test_gold_text_scores_perfectly_and_unknown_files_are_ignored(self) -> None:
        manifest = json.loads((ROOT / "fixtures" / "manifest.json").read_text())
        entry = next(f for f in manifest["fixtures"] if f["id"] == "two-column-newsletter")
        gold = json.loads((ROOT / "fixtures" / entry["gold"]).read_text())
        export = [
            {
                "title": "t",
                "version": 1,
                "status": "ready",
                "parser": "docling-x",
                "parse_notes": {},
                "text_sha256": "x",
                "byte_sha256": entry["sha256"],
                "out": {"text": gold["text"], "tables": [], "headings": gold["headings"]},
            },
            {
                "title": "other",
                "version": 1,
                "status": "ready",
                "parser": "p",
                "parse_notes": {},
                "text_sha256": "y",
                "byte_sha256": "0" * 64,
            },
            {
                "title": "scan",
                "version": 1,
                "status": "needs_ocr",
                "parser": None,
                "parse_notes": {},
                "text_sha256": None,
                "byte_sha256": next(f["sha256"] for f in manifest["fixtures"] if f["id"] == "scanned-delivery-note"),
            },
        ]
        rows = score_export(export)
        self.assertEqual([r["fixture"] for r in rows], ["two-column-newsletter", "scanned-delivery-note"])
        self.assertEqual(rows[0]["metrics"]["reading_order"], 1.0)
        self.assertEqual(rows[0]["metrics"]["heading_recall"], 1.0)
        self.assertEqual(rows[0]["m1_docling"]["reading_order"], 1.0)
        self.assertNotIn("metrics", rows[1])
        self.assertIn("needs_ocr", markdown(rows))


if __name__ == "__main__":
    unittest.main()
