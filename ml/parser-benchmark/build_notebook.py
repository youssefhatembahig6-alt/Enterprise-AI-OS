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
   The two environments are independent: if one fails to install or crashes, the other still runs to the end.
3. Cell 5 prints the batch summary: each of the 24 expected adapter/fixture pairs is *succeeded*, *failed* (with the
   failed stage: `install`, `runner` or `parse`) or *missing*. The results tables are shown only if the report for
   this batch was generated successfully.
4. Cell 6 downloads `eaios-m1-parsers-<batch>.zip`, also after a failure (it then holds the partial results, logs
   and `status.json`). If the browser blocks the download, open the **Files** panel and download
   `/content/eaios-m1-parsers-<batch>.zip` from there, or rerun cell 6 alone.
5. Unzip it into `ml/parser-benchmark/results/` on the branch, then run `python -m bench.report` and commit the
   results and the regenerated tables.

The notebook clones branch `{BRANCH}`. After it is merged, change `BRANCH` in cell 2 to `main`.
"""

CELL_ENV = r"""# 1. Record the runtime and define helpers (nothing is installed yet)
import datetime, json, os, platform, subprocess, sys, uuid
BATCH = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]

def sh(cmd):
    print("$", cmd)
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    print(r.stdout[-4000:], r.stderr[-4000:])
    return r

def run_logged(cmd, log_path, cwd=None):
    # Stream a command to the cell output and to a log file; return its exit code.
    print("$", cmd)
    with open(log_path, "a") as log:
        log.write(f"$ {cmd}\n")
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in p.stdout:
            print(line, end="")
            log.write(line)
        return p.wait()

def tail(path, n=40):
    try:
        with open(path) as fh:
            return "".join(fh.readlines()[-n:])
    except OSError:
        return ""

gpu = sh("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader").stdout.strip()
print(json.dumps({"batch": BATCH, "python": platform.python_version(), "gpu": gpu or None,
                  "cpu_count": os.cpu_count()}, indent=1))
if not gpu:
    print("WARNING: no GPU attached. The run continues on CPU and records that.")
"""

CELL_CLONE = f'''# 2. Clone the repository, verify the fixtures, open this batch's status file
import hashlib
BRANCH = "{BRANCH}"
REPO = "{REPO}"
WORK = "/content/eaios"
if not os.path.isdir(WORK):
    sh(f"git clone --depth 1 -b {{BRANCH}} {{REPO}} {{WORK}}")
else:
    sh(f"git -C {{WORK}} fetch --depth 1 origin {{BRANCH}} && git -C {{WORK}} checkout -q FETCH_HEAD")
BENCH = f"{{WORK}}/ml/parser-benchmark"
print(sh(f"git -C {{WORK}} rev-parse HEAD").stdout)
with open(f"{{BENCH}}/fixtures/manifest.json") as fh:
    manifest = json.load(fh)
for e in manifest["fixtures"]:
    for key, rel in (("sha256", e["file"]), ("gold_sha256", e["gold"])):
        with open(f"{{BENCH}}/fixtures/{{rel}}", "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == e[key], f"hash mismatch: {{rel}}"
print(len(manifest["fixtures"]), "fixtures verified by SHA-256")
sys.path.insert(0, BENCH)
from bench import colab_status as cs  # standard library only
STATUS = cs.Status(f"{{BENCH}}/results/{{BATCH}}")
print(len(cs.expected_pairs()), "expected Colab adapter/fixture pairs; status file:", STATUS.path)
'''

CELL_INSTALL = r"""# 3. Install each environment separately and record the result (a failure does not stop the other)
sh("apt-get -qq update && apt-get -qq install -y tesseract-ocr poppler-utils libmagic1 > /dev/null")
sh("pip -q install uv==0.9.5")
SMOKE = {"docling": "import docling.document_converter, pymupdf, pymupdf4llm, rapidfuzz",
         "unstructured": "import unstructured.partition.pdf, unstructured.partition.xlsx, rapidfuzz"}
for name in cs.ENV_ADAPTERS:
    venv, log = f"/content/venv-{name}", f"{STATUS.dir}/install-{name}.log"
    rc, detail = None, ""
    try:
        rc = 0 if os.path.exists(f"{venv}/bin/python") else run_logged(f"uv venv --python 3.12 {venv}", log)
        if rc == 0:
            rc = run_logged(f"uv pip install --python {venv}/bin/python -r {BENCH}/colab-{name}-pins.txt", log)
        if rc == 0:  # installed is not enough: the packages must import
            rc = run_logged(f"cd {BENCH} && {venv}/bin/python -c '{SMOKE[name]}'", log)
        if rc == 0 and name == "unstructured":
            rc = run_logged("tesseract --version", log)
        if rc == 0:
            with open(f"{STATUS.dir}/freeze-{name}.txt", "w") as fh:
                fh.write(sh(f"uv pip freeze --python {venv}/bin/python").stdout)
        detail = tail(log)
    except Exception as exc:  # record and continue with the next environment
        detail = f"{type(exc).__name__}: {exc}"
    STATUS.record(name, "install", rc == 0, rc, detail, log)
    print(f"== {name} install:", "ok" if rc == 0 else f"FAILED (exit {rc}); see {log}")
"""

CELL_RUN = r"""# 4. Run each environment's adapters (an environment that failed to install is skipped and recorded)
os.environ["DO_NOT_TRACK"] = "true"; os.environ["SCARF_NO_ANALYTICS"] = "true"
for name, adapters in cs.ENV_ADAPTERS.items():
    install = STATUS.stage(name, "install")
    if not install or not install["ok"]:
        STATUS.record(name, "run", False, None, "skipped: the install stage failed")
        print(f"== {name} run: skipped (install failed)")
        continue
    log = f"{STATUS.dir}/run-{name}.log"
    rc, detail = None, ""
    try:
        rc = run_logged(f"/content/venv-{name}/bin/python -m bench.run --where colab --adapters {','.join(adapters)} "
                        f"--runs 3 --batch {BATCH} --out results", log, cwd=BENCH)
        detail = tail(log)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
    STATUS.record(name, "run", rc == 0, rc, detail, log)
    print(f"== {name} run:", "ok" if rc == 0 else f"FAILED (exit {rc}); see {log}")
"""

CELL_REPORT = r"""# 5. Batch summary (all 24 expected pairs), then the results tables if the report succeeded
from IPython.display import Markdown, display
tables = cs.run_report(STATUS, BENCH, BATCH)  # writes results/<batch>/results-tables.md, or records the failure
summary = cs.write_summary(STATUS, f"{BENCH}/results", BATCH)
display(Markdown(cs.summary_markdown(summary)))
if tables is not None:
    with open(tables) as fh:
        display(Markdown(fh.read()))
else:
    print("Report generation FAILED for this batch, so no results tables are shown.")
    print(STATUS.data["report"]["detail"][-3000:])
"""

CELL_DOWNLOAD = r"""# 6. Download this batch's results as a ZIP (works after any failure; rerun this cell alone if needed)
import glob, shutil
if "BATCH" not in globals():
    print("Run cell 1 first.")
else:
    stage = f"/content/eaios-m1-parsers-{BATCH}"
    os.makedirs(stage, exist_ok=True)
    bench = globals().get("BENCH")
    if bench and os.path.isdir(f"{bench}/results"):
        for p in glob.glob(f"{bench}/results/*.json"):
            try:
                with open(p) as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("batch") == BATCH:
                shutil.copy(p, stage)
                outputs = p[: -len(".json")]
                if os.path.isdir(outputs):
                    shutil.copytree(outputs, f"{stage}/{os.path.basename(outputs)}", dirs_exist_ok=True)
        if os.path.isdir(f"{bench}/results/{BATCH}"):  # status.json, summary, logs, freezes, tables
            shutil.copytree(f"{bench}/results/{BATCH}", f"{stage}/{BATCH}", dirs_exist_ok=True)
    else:
        print("The repository was not cloned; the ZIP holds no results.")
    zip_path = shutil.make_archive(stage, "zip", stage)
    print(zip_path, os.path.getsize(zip_path), "bytes")
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
