# M1 parser comparison: laptop results and recommendation

Laptop run `20261120T211830Z-b9dfec`, 2026-11-20. The full generated tables are in [`results-tables.md`](results-tables.md) and the raw results are in [`../results/`](../results/). The Colab half (Docling, the Unstructured PDF strategies and `partition_xlsx`, and PyMuPDF with its layout model) has **not run yet**. Every Colab cell below is marked pending, not estimated.

## Recommendation

| Input | Initial parser | Fallback | Status |
| --- | --- | --- | --- |
| Digital PDF (has a text layer) | **PyMuPDF 2.1, heuristic mode**: `get_text("text")` with `find_tables(use_layout=False)` and `pymupdf._layout_enabled = False` | **pdfplumber 0.11.10**: an independent engine (pdfminer). Use it when PyMuPDF raises or returns no text from a page that has a text layer. Also use it for geometry debugging. | Measured on the laptop |
| Scanned PDF (no text layer) | **None yet.** Detect the scan, mark the document "needs OCR", and do not index it. | — | Waiting for the Colab run |
| Multi-column or layout-dependent PDF | Index it with PyMuPDF, but **do not claim reading order**: these results show PyMuPDF follows the PDF content stream, not the layout. | — | The layout-model candidates are waiting for the Colab run |
| CSV | **Python `csv`**: decode the BOM, then strict UTF-8, then cp1252 (never latin-1). Detect the delimiter with `csv.Sniffer`. Keep every field as text. | — | Measured on the laptop |
| XLSX | **openpyxl**, loaded twice: `data_only=True` for cached values and `data_only=False` for formulas. Keep `sheet_state` and merged ranges. | pandas `read_excel` for bulk values (it gives the same values, but no formulas and no hidden flag) | Measured on the laptop |

The specification's main question is still open: **does Docling or Unstructured win as the production parser?** No local result can answer it, because both need learned models that must stay off laptops. The choice above is the initial parser for M3's digital fixtures. It matches ADR 0016's documented digital-PDF fallback, and nothing more.

## What was measured

**Seven fixtures.** All are synthetic and public. Each file's SHA-256 is recorded in [`../fixtures/manifest.json`](../fixtures/manifest.json), and all of them regenerate byte for byte.

| Fixture | Case | What makes it hard |
| --- | --- | --- |
| `digital-company-overview.pdf` | digital | Headings, bullets, a numbered list. Built from the PUBLIC `about.md` plus a labelled synthetic section. |
| `two-column-newsletter.pdf` | two-column | A paragraph crosses the column break, and a full-width note sits below the columns. **The content stream is drawn out of reading order** (footer, note, right column, title, left column). |
| `table-heavy-sales-q1-2026.pdf` | table-heavy | Four tables built from the vendored sales data: a full grid (T1), a borderless table (T2), a merged two-level header (T3), and a zebra table with horizontal rules only that splits across a page break with a repeated header (T4). Totals match the frozen Q1 figures (80,934,385.00 net, 291 orders). |
| `scanned-delivery-note.pdf` | scanned | An image-only page at 200 dpi, rotated 0.8°, with speckle noise and blur. It holds a ruled line-item table and has no text layer. |
| `tricky-customers.csv` | CSV | UTF-8 with a BOM, CRLF line endings, quoted commas, doubled quotes, an embedded newline, Arabic, an emoji, leading-zero codes, and formula-like text (`=1+1 Holdings`) |
| `semicolon-cp1252.csv` | CSV | Semicolon delimiter, cp1252 encoding (the euro sign is byte 0x80), decimal commas, a quoted delimiter |
| `multi-sheet-q1.xlsx` | workbook | Four sheets, one of them hidden. A merged title, `COUNTIF`/`SUMIF`/`SUM` formulas with cached values, typed dates, wrapped text, Arabic, and a text value `00123`. |

**Metrics.** Text similarity is order-sensitive (normalized Levenshtein). Word recall and precision measure completeness. Reading order is the longest in-order run of gold paragraph anchors. Table cells counts body cells in the right row, in the right column order; a row only counts when its key cell is present. Spurious tables counts tables reported on pages that have none. Spreadsheets get exact field and cell checks, plus formulas, the hidden-sheet flag, and the merged title. Runtime is the median of 5 warm runs after one cold run.

**Hardware.** Intel Core i5-13420H (12 threads), 15.3 GiB RAM, no GPU, Debian 13, Python 3.13.5.

## Results (laptop)

| Parser | Digital: text sim. / reading order | Two-column: text sim. / reading order | Table-heavy: table cells (T1 / T2 / T3 / T4) | Scanned | Median time, digital / table-heavy |
| --- | --- | --- | --- | --- | --- |
| PyMuPDF (no layout) | **100.0 / 100.0** | 21.9 / 47.1 | 22.6 (100 / 0 / 100 / 0) | 0 (no text) | 21 ms / 58 ms |
| PyMuPDF `sort=True` | 100.0 / 100.0 | 36.0 / 64.7 | 22.6 (100 / 0 / 100 / 0) | 0 | 26 ms / 67 ms |
| pdfplumber | 95.6 / 100.0 | 36.0 / 64.7 | 22.6 (100 / 0 / 100 / 0) | 0 | 25 ms / 64 ms |
| PyMuPDF / pdfplumber, text-strategy tables | same text as above | same | 31.6 (20 / 0 / 0 / 42), and **1 spurious table** on each prose page | 0 | 44–49 ms / 123–129 ms |
| Docling, Unstructured fast / hi_res / ocr_only, PyMuPDF + layout | **pending (Colab)** | pending | pending | pending | pending |

| Parser | `tricky-customers.csv` exact fields | `semicolon-cp1252.csv` | Workbook: cell values / formulas / hidden sheet |
| --- | --- | --- | --- |
| Python `csv` (native) | **100.0**, all checks pass | **100.0**, all checks pass | — |
| pandas `read_csv` (defaults) | 100.0 | **fails**: `UnicodeDecodeError` | — |
| Unstructured `partition_csv` | 96.8 (loses the embedded newline) | **fails**: `UnicodeDecodeError` | — |
| openpyxl | — | — | **100.0 / 100.0 / flagged** |
| pandas `read_excel` | — | — | 100.0 / n/a / not flagged |
| Unstructured `partition_xlsx` | — | — | pending (Colab: it loads a spaCy model) |
| Docling (CSV, XLSX) | pending (Colab) | pending | pending |

The routing check (spec step 1) classified all four PDFs correctly. The scan has 0 text characters and 1 full-page image, so it routes to OCR plus layout.

## Measured limitations of the recommendation

1. **Reading order follows the content stream.** On the two-column page, PyMuPDF kept every word (100% recall and precision) but put the right column, the title and the left column in the order the PDF drew them (reading order 47.1). `sort=True` and pdfplumber interleave the two columns line by line instead (64.7). None of the laptop parsers reconstructs the column layout. Real PDFs from layout tools often have this problem; the generated digital fixtures here do not.
2. **Only ruled tables are found.** The default line-based finders recovered T1 and T3 exactly, including the body under the merged header. They found nothing in the borderless T2 or the rules-only T4. Table text is still complete (100% of cell values appear in the text), so values are searchable but not structured. The text strategy is worse, not better: it turns the whole page into one grid, splits words (`N` | `IL-0011`, `Document Int` | `elligence`), and reports a table on every prose page.
3. **Merged headers lose their structure.** For T3, the top header row comes back as `Region, Q1 2026` and the second row as `"", January, …`. Header accuracy is 33.3 over all four tables.
4. **No OCR without models.** Every laptop parser returns nothing for the scan. Tesseract is a learned (LSTM) OCR engine, so it also belongs on Colab under the current constraint.
5. **pdfplumber glyph mapping.** pdfplumber emits `(cid:127)` for the bullet glyph in the embedded DejaVu subset. This explains its 95.6 text similarity on the digital PDF.
6. **PyMuPDF 2.x runs a model by default.** `find_tables()` calls an ONNX layout model unless `use_layout=False`, and `pymupdf_layout` and `onnxruntime` are hard dependencies of `pymupdf==2.1`. The laptop adapter disables the model. `tests/test_guard.py` runs every laptop adapter in a fresh interpreter and fails if `onnxruntime`, `torch`, `pymupdf.layout`, `unstructured_inference` or `docling` was imported. Ingestion code must keep the same setting.
7. **CSV and spreadsheet semantics need the native path.** Default pandas and Unstructured cannot decode cp1252. Unstructured also flattens the embedded newline. Only openpyxl exposes formulas and the hidden-sheet flag; pandas reads the hidden sheet without flagging it. Hidden sheets can hold data that should not be indexed silently.
8. **Unstructured `partition_xlsx` runs a learned model.** It downloads spaCy's `en_core_web_sm` at run time and loads it (it imports spaCy even with `find_subtable=False`). It is therefore a Colab-only candidate under the current constraint.

## Run-integrity note

The first trial runs on the laptop (results discarded, never committed) included Unstructured `partition_xlsx` as a laptop adapter. At that point the guard list did not include spaCy, and the call downloaded `en_core_web_sm` from GitHub and loaded it on the laptop once per run. The fix has three parts: spaCy, thinc, `en_core_web_sm`, NLTK and transformers are now in the guard list; the adapter moved to Colab; and the downloaded model was uninstalled from the benchmark venv. The committed run was made afterwards, and it records `model_modules_loaded: []` for every laptop result. No other laptop adapter imports any listed module (`tests/test_guard.py`).

## Limits of this benchmark

- Seven small synthetic files, all generated with reportlab, Pillow and XlsxWriter. Only the two-column fixture has an adversarial content stream. There are no Arabic PDFs, no real scans, no forms, and no DOCX (DOCX upload is in FR scope but not in the roadmap's benchmark list).
- The gold references come from the same data structures that draw the pages. The implementation agent checked them by eye against page images rendered with `pdftoppm` (the procedure is in [`../fixtures/README.md`](../fixtures/README.md)). No team member has checked them yet: **a human check is still outstanding**.
- Reading-order anchors are paragraph-level, so line-level disorder inside a paragraph shows only in text similarity.
- Timings come from one laptop with a warm file cache. They compare parsers; they do not predict throughput.
- The Colab notebook is generated and checked in CI, but it **has not been executed**. Its pins (`colab-*-pins.txt`) are top-level only, so dependency resolution on Colab is unverified. The notebook saves the full freeze with its results.
- Amazon Textract and Azure AI Document Intelligence were not run. The specification allows them only where cloud processing is permitted, and no such permission or account exists.

## Next step

Run [`../m1_parser_benchmark.ipynb`](../m1_parser_benchmark.ipynb) on Colab and commit its results. Then run `python -m bench.report` and revisit two rows of the recommendation: scanned PDFs, and the layout-dependent production parser (Docling versus Unstructured `hi_res`).
