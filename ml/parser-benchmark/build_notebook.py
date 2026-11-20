"""Generate m1_parser_benchmark.ipynb (the Colab half of the parser benchmark).

    python ml/parser-benchmark/build_notebook.py           # write the notebook
    python ml/parser-benchmark/build_notebook.py --check   # CI: committed notebook matches

The notebook is thin: it clones this repository, installs two pinned environments, and runs
the same ``bench.run`` code that the laptop run uses, with ``--where colab``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NOTEBOOK = HERE / "m1_parser_benchmark.ipynb"
BRANCH = "feature/m1-parser-comparison"
REPO = "https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git"

INTRO = f"""# M1 parser benchmark: Colab models

This notebook runs the parser candidates that use **learned layout, table or OCR models**. They must not
run on team laptops. The laptop half of the benchmark (PyMuPDF without its layout model, pdfplumber,
csv, pandas, openpyxl, Unstructured CSV) is already committed in `ml/parser-benchmark/results/`.

| Environment | Adapters | Models downloaded on first use |
| --- | --- | --- |
| `/content/venv-docling` | `docling` (PDF, CSV, XLSX), `pymupdf_layout` | Docling layout model, TableFormer, Docling's default OCR engine; PyMuPDF's ONNX layout model |
| `/content/venv-unstructured` | `unstructured_fast`, `unstructured_hi_res`, `unstructured_ocr_only`, `unstructured_xlsx` | Unstructured layout and table models; Tesseract (apt); spaCy `en_core_web_sm` (XLSX) |

The fixtures are the repository's **synthetic, public** fixtures, verified by SHA-256 before anything runs.
No credentials, Drive access or tunnels are used.

**Steps**

1. **File → Save a copy in Drive**, then **Runtime → Change runtime type → T4 GPU → Save**. A CPU runtime also works
   but is slower; the hardware is recorded either way.
2. **Runtime → Run all.** It takes about 15–30 minutes, most of it installing packages and downloading models.
3. Cell 6 downloads `eaios-m1-parsers-<batch>.zip`. Unzip it into `ml/parser-benchmark/results/` on the branch, then run
   `python -m bench.report` and commit the results and the regenerated tables.

The notebook clones branch `{BRANCH}`. After it is merged, change `BRANCH` in cell 2 to `main`.
"""

CELL_ENV = r"""# 1. Record the runtime before installing anything
import datetime, json, os, platform, subprocess, uuid
BATCH = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
def sh(cmd):
    print("$", cmd); r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    print(r.stdout[-4000:], r.stderr[-4000:]); return r
gpu = sh("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader").stdout.strip()
print(json.dumps({"batch": BATCH, "python": platform.python_version(), "gpu": gpu or None,
                  "cpu_count": os.cpu_count()}, indent=1))
if not gpu:
    print("WARNING: no GPU attached. The run continues on CPU and records that.")
"""

CELL_CLONE = f'''# 2. Clone the repository and verify the fixtures
BRANCH = "{BRANCH}"
REPO = "{REPO}"
WORK = "/content/eaios"
if not os.path.isdir(WORK):
    sh(f"git clone --depth 1 -b {{BRANCH}} {{REPO}} {{WORK}}")
else:
    sh(f"git -C {{WORK}} fetch --depth 1 origin {{BRANCH}} && git -C {{WORK}} checkout -q FETCH_HEAD")
BENCH = f"{{WORK}}/ml/parser-benchmark"
print(sh(f"git -C {{WORK}} rev-parse HEAD").stdout)
import hashlib
manifest = json.load(open(f"{{BENCH}}/fixtures/manifest.json"))
for e in manifest["fixtures"]:
    for key, rel in (("sha256", e["file"]), ("gold_sha256", e["gold"])):
        digest = hashlib.sha256(open(f"{{BENCH}}/fixtures/{{rel}}", "rb").read()).hexdigest()
        assert digest == e[key], f"hash mismatch: {{rel}}"
print(len(manifest["fixtures"]), "fixtures verified by SHA-256")
'''

CELL_INSTALL = r"""# 3. System packages and two pinned environments (Docling and Unstructured kept apart)
sh("apt-get -qq update && apt-get -qq install -y tesseract-ocr poppler-utils libmagic1 > /dev/null")
sh("pip -q install uv==0.9.5")
for name in ("docling", "unstructured"):
    venv = f"/content/venv-{name}"
    if not os.path.exists(f"{venv}/bin/python"):
        sh(f"uv venv --python 3.12 {venv}")
    r = sh(f"uv pip install --python {venv}/bin/python -r {BENCH}/colab-{name}-pins.txt")
    print(name, "install exit code", r.returncode)
    os.makedirs(f"{BENCH}/results/{BATCH}", exist_ok=True)
    with open(f"{BENCH}/results/{BATCH}/freeze-{name}.txt", "w") as fh:
        fh.write(sh(f"uv pip freeze --python {venv}/bin/python").stdout)
sh("tesseract --version | head -1")
"""

CELL_RUN = r"""# 4. Run the model-based adapters (one environment at a time; a failure in one does not stop the other)
os.environ["DO_NOT_TRACK"] = "true"; os.environ["SCARF_NO_ANALYTICS"] = "true"
PLAN = [("docling", "docling,pymupdf_layout"),
        ("unstructured", "unstructured_fast,unstructured_hi_res,unstructured_ocr_only,unstructured_xlsx")]
for venv, adapters in PLAN:
    cmd = (f"cd {BENCH} && /content/venv-{venv}/bin/python -m bench.run --where colab "
           f"--adapters {adapters} --runs 3 --batch {BATCH} --out results")
    print("$", cmd)
    p = subprocess.run(cmd, shell=True, text=True)
    print(venv, "exit code", p.returncode)
"""

CELL_REPORT = r"""# 5. Merge with the committed laptop run and show the tables
sh(f"cd {BENCH} && python -m pip -q install rapidfuzz==3.14.6 && python -m bench.report")
from IPython.display import Markdown, display
display(Markdown(open(f"{BENCH}/reports/results-tables.md").read()))
"""

CELL_DOWNLOAD = r"""# 6. Download the results (safe to run at any time, including after a failure)
import glob, shutil
stage = f"/content/eaios-m1-parsers-{BATCH}"
os.makedirs(stage, exist_ok=True)
for p in glob.glob(f"{BENCH}/results/*.json"):
    if BATCH in open(p).read():
        shutil.copy(p, stage)
if os.path.isdir(f"{BENCH}/results/{BATCH}"):
    shutil.copytree(f"{BENCH}/results/{BATCH}", f"{stage}/{BATCH}", dirs_exist_ok=True)
for d in glob.glob(f"{BENCH}/results/*/outputs"):
    run_dir = os.path.dirname(d)
    if any(BATCH in open(j).read() for j in glob.glob(f"{run_dir}.json")):
        shutil.copytree(run_dir, f"{stage}/{os.path.basename(run_dir)}", dirs_exist_ok=True)
zip_path = shutil.make_archive(stage, "zip", stage)
print(zip_path)
try:
    from google.colab import files
    files.download(zip_path)
except ImportError:
    pass
"""


def _cell(kind: str, src: str) -> dict:
    lines = src.strip("\n").splitlines(keepends=True)
    cell: dict = {"cell_type": kind, "metadata": {}, "source": lines}
    if kind == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def build() -> str:
    nb = {
        "cells": [
            _cell("markdown", INTRO),
            _cell("code", CELL_ENV),
            _cell("code", CELL_CLONE),
            _cell("code", CELL_INSTALL),
            _cell("code", CELL_RUN),
            _cell("code", CELL_REPORT),
            _cell("code", CELL_DOWNLOAD),
        ],
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": [], "gpuType": "T4"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 0,
    }
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = build()
    if args.check:
        if NOTEBOOK.read_text(encoding="utf-8") != text:
            print("notebook is stale; run python ml/parser-benchmark/build_notebook.py")
            return 1
        print("notebook matches its generator")
        return 0
    NOTEBOOK.write_text(text, encoding="utf-8")
    print(f"wrote {NOTEBOOK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
