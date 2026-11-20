"""Explicit run selection for the committed results tables (results/selection.json)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bench import report
from bench.adapters import ADAPTERS

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"


class CommittedSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.paths, self.sel = report.load_selection(RESULTS)
        self.text = report.build(self.paths, selection=self.sel)

    def test_every_adapter_comes_from_exactly_one_named_run(self) -> None:
        owners = [a for e in self.sel["selected"] for a in e["adapters"]]
        self.assertEqual(len(owners), len(set(owners)))
        self.assertEqual(set(owners), set(ADAPTERS))

    def test_selected_runs_are_the_reviewed_ones(self) -> None:
        by_run = {e["run"]: set(e["adapters"]) for e in self.sel["selected"]}
        self.assertEqual(by_run["20261120T220551Z-200026"], {"docling", "pymupdf_layout"})
        self.assertEqual(by_run["20261120T220043Z-890d9e"], {"unstructured_hi_res", "unstructured_ocr_only"})
        self.assertEqual(by_run["20261120T220748Z-49b6cc"], {"unstructured_fast", "unstructured_xlsx"})

    def test_failed_results_left_out_are_listed_not_dropped(self) -> None:
        doc = json.loads((RESULTS / "20261120T220748Z-49b6cc.json").read_text(encoding="utf-8"))
        failed = [r for r in doc["results"] if r["status"] == "failed"]
        self.assertEqual(len(failed), 8)
        section = self.text.split("### Excluded results", 1)[1].split("\n## ", 1)[0]
        for r in failed:
            self.assertIn(f"| `20261120T220748Z-49b6cc` | `{r['adapter']}` | {r['fixture']} | failed |", section)
        self.assertIn("matplotlib_inline", section)

    def test_no_pair_is_pending_and_laptop_failures_stay_visible(self) -> None:
        self.assertIn("Every planned adapter/fixture pair has a result.", self.text)
        self.assertIn("failed: UnicodeDecodeError", self.text)  # selected failures are reported as failures

    def test_original_colab_zip_is_kept_unchanged(self) -> None:
        import hashlib

        z = self.sel["colab_zip"]
        data = (RESULTS / "colab-zips" / z["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), z["sha256"])


class SelectionValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        for p in RESULTS.glob("*.json"):
            shutil.copy(p, self.tmp / p.name)
        self.sel = json.loads((RESULTS / "selection.json").read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def write(self) -> None:
        (self.tmp / "selection.json").write_text(json.dumps(self.sel))

    def test_adapter_selected_from_two_runs_is_an_error(self) -> None:
        # e.g. trying to take hi_res from both the failed and the successful run
        entry = next(e for e in self.sel["selected"] if e["run"] == "20261120T220748Z-49b6cc")
        entry["adapters"].append("unstructured_hi_res")
        self.write()
        with self.assertRaisesRegex(report.SelectionError, "selected from both"):
            report.load_selection(self.tmp)

    def test_missing_run_file_is_an_error(self) -> None:
        self.sel["selected"][1]["run"] = "20991231T000000Z-nosuch"
        self.write()
        with self.assertRaisesRegex(report.SelectionError, "no results file"):
            report.load_selection(self.tmp)

    def test_adapter_absent_from_the_named_run_is_an_error(self) -> None:
        entry = next(e for e in self.sel["selected"] if e["run"] == "20261120T220043Z-890d9e")
        entry["adapters"].append("unstructured_fast")
        fast_owner = next(e for e in self.sel["selected"] if e["run"] == "20261120T220748Z-49b6cc")
        fast_owner["adapters"].remove("unstructured_fast")
        self.write()
        with self.assertRaisesRegex(report.SelectionError, "has no results for adapter unstructured_fast"):
            report.load_selection(self.tmp)

    def test_unselected_adapter_is_reported_pending_not_filled_in(self) -> None:
        entry = next(e for e in self.sel["selected"] if e["run"] == "20261120T220043Z-890d9e")
        entry["adapters"].remove("unstructured_ocr_only")
        self.write()
        paths, sel = report.load_selection(self.tmp)
        text = report.build(paths, selection=sel)
        self.assertIn("`unstructured_ocr_only` × `scanned-delivery-note`", text.split("## Pending and not run")[1])
        self.assertNotIn("| Unstructured ocr_only | colab | 90.2", text)


if __name__ == "__main__":
    unittest.main()
