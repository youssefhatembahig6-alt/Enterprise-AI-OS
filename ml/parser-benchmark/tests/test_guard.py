"""Laptop adapters must never load a learned-model runtime.

PyMuPDF 2.x runs an ONNX layout model inside find_tables() unless use_layout=False. This test
runs every laptop adapter on every applicable fixture in a fresh interpreter and fails if any
model runtime module was imported.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import json, sys
from pathlib import Path
from bench.adapters import ADAPTERS, MODEL_MODULES
root = Path(sys.argv[1])
manifest = json.loads((root / "fixtures/manifest.json").read_text())
for a in ADAPTERS.values():
    if a.where != "laptop":
        continue
    for e in manifest["fixtures"]:
        if e["case"] in a.cases:
            try:
                a.run(root / "fixtures" / e["file"])
            except Exception:  # expected parser failures (e.g. pandas on cp1252) are results
                pass
print(json.dumps(sorted(m for m in MODEL_MODULES if m in sys.modules)))
"""


class LaptopGuard(unittest.TestCase):
    def test_no_model_runtime_imported(self) -> None:
        env = {
            "DO_NOT_TRACK": "true",
            "SCARF_NO_ANALYTICS": "true",
            "UNSTRUCTURED_DISABLE_NOTICE": "1",
            "PATH": "/usr/bin:/bin",
        }
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT)], cwd=ROOT, capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), [])

    def test_colab_adapters_are_marked_as_using_models(self) -> None:
        from bench.adapters import ADAPTERS

        for a in ADAPTERS.values():
            if a.name in (
                "docling",
                "unstructured_hi_res",
                "unstructured_ocr_only",
                "unstructured_xlsx",
                "pymupdf_layout",
            ):
                self.assertEqual(a.where, "colab", a.name)
                self.assertNotEqual(a.uses_models, "none", a.name)


class NotebookAndTables(unittest.TestCase):
    def test_notebook_matches_generator(self) -> None:
        r = subprocess.run([sys.executable, str(ROOT / "build_notebook.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_results_tables_are_current(self) -> None:
        r = subprocess.run([sys.executable, "-m", "bench.report", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
