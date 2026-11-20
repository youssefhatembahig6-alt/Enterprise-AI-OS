# M1 parser benchmark

This benchmark compares the parser candidates from the specification's document-extraction section on the roadmap's cases: digital, two-column, table-heavy and scanned PDFs, a tricky CSV and a multi-sheet workbook. It scores each parser against hand-checked gold text, reading order and table values, and records runtime, failures, versions and hardware.

* **Report and recommendation:** [`reports/2026-11-20-parser-comparison.md`](reports/2026-11-20-parser-comparison.md)
* **Generated tables:** [`reports/results-tables.md`](reports/results-tables.md)
* **Fixtures, provenance and the gold-check procedure:** [`fixtures/README.md`](fixtures/README.md)
* **Which run supplies each adapter's results:** [`results/selection.json`](results/selection.json). It is an explicit, reviewed mapping, validated by `bench.report`. Every result it leaves out is listed in the tables. The original Colab ZIP is kept in `results/colab-zips/`.

## Where each candidate runs

| Runs on the laptop (no learned models) | Runs on Colab only (learned layout, table or OCR models) |
| --- | --- |
| PyMuPDF 2.1 with `use_layout=False` (also `sort=True` and text-strategy variants), pdfplumber (default and text-strategy tables), Python `csv`, pandas, openpyxl, Unstructured `partition_csv` | Docling (PDF, CSV, XLSX), Unstructured `fast` / `hi_res` / `ocr_only` / `partition_xlsx`, PyMuPDF + its ONNX layout model |

Unstructured `fast` uses no model, but importing Unstructured's PDF module requires `unstructured-inference`, which installs a multi-GB CUDA torch stack. So it runs on Colab too. Unstructured `partition_xlsx` downloads and loads the spaCy model `en_core_web_sm` at run time, so it runs on Colab as well. Docling's CSV and XLSX paths also use no model, but installing Docling pulls in torch, so they run with the rest of Docling on Colab. Amazon Textract and Azure AI Document Intelligence are cloud services; they are not run (see the report).

## Reproduce the laptop half

```bash
cd ml/parser-benchmark
python3 -m venv .venv
.venv/bin/pip install -r requirements-laptop.txt
.venv/bin/python fixtures/generate.py --check        # fixtures reproduce byte-for-byte
.venv/bin/python -m unittest discover -s tests -v    # scorer, fixtures, no-model guard, notebook, tables
.venv/bin/python -m bench.run --where laptop --runs 5
.venv/bin/python -m bench.report                     # rewrites reports/results-tables.md
```

`bench.run` checks every fixture's SHA-256 before it starts. It refuses to run, and aborts mid-run, if a model runtime (`onnxruntime`, `torch`, `transformers`, `pymupdf.layout`, `unstructured_inference`, `docling`, `spacy`, `nltk` and others) is imported. It also sets `DO_NOT_TRACK` and `SCARF_NO_ANALYTICS`, so Unstructured sends no usage ping. Each run writes `results/<run_id>.json` (environment, package versions, per-pair cold and warm times, metrics and errors) and keeps each parser's output under `results/<run_id>/outputs/` for inspection.

## Run the Colab half

Open [`m1_parser_benchmark.ipynb`](m1_parser_benchmark.ipynb) in Colab:
<https://colab.research.google.com/github/youssefhatembahig6-alt/Enterprise-AI-OS/blob/feature/m1-parser-comparison/ml/parser-benchmark/m1_parser_benchmark.ipynb>

Choose a T4 runtime, then **Run all**. The notebook clones this branch and verifies the fixture hashes. It then installs Docling and Unstructured into separate pinned uv environments (`colab-docling-pins.txt`, `colab-unstructured-pins.txt`), runs `bench.run --where colab`, and downloads a zip. Keep the ZIP in `results/colab-zips/` and unzip it into `results/`. Name the runs to use in `results/selection.json`; the report never picks runs on its own. Then run `.venv/bin/python -m bench.report` and commit everything.

The notebook passes `MPLBACKEND=Agg` to every benchmark subprocess. Colab's inline backend broke Unstructured `hi_res`/`ocr_only` in batch `20261120T220024Z-907146`.

Partial runs are explicit. Each environment's `install` and `run` stages are recorded separately in `results/<batch>/status.json`, together with their logs, and an environment that fails does not stop the other. The runner rewrites its results file after every pair, so a crash keeps the finished pairs. Cell 5 classifies all 24 expected pairs as succeeded, failed (naming the `install`, `runner` or `parse` stage) or missing. It shows the results tables only when the report for that batch was generated, into `results/<batch>/results-tables.md`. Cell 6 builds the ZIP from whatever exists, even after a failure. The logic is in `bench/colab_status.py` and is tested in `tests/test_colab_status.py`.

The notebook is generated: edit [`build_notebook.py`](build_notebook.py), then run `python build_notebook.py`. CI fails if the two differ.

## Layout

| Path | Purpose |
| --- | --- |
| `fixtures/generate.py` | Deterministic fixture and gold generator |
| `fixtures/files/`, `fixtures/gold/`, `fixtures/manifest.json` | Committed fixtures, gold references, hashes and provenance |
| `bench/adapters.py` | One adapter per candidate, with a common output shape and run-location metadata |
| `bench/scoring.py` | Metric definitions (see the module docstring) |
| `bench/run.py`, `bench/report.py` | Runner and table generator |
| `bench/colab_status.py` | Notebook stage tracking and the 24-pair batch summary (standard library only) |
| `bench/rescore.py` | Re-applies the current scorer to saved parser outputs and records every changed metric (`--check` compares without writing) |
| `results/` | Committed raw results, including parser outputs |
| `tests/` | `unittest` suite, run in CI |
