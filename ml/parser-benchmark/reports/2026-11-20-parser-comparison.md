# M1 parser comparison: measured results and recommendation

This compares the specification's parser candidates on seven synthetic fixtures. The laptop run is `20261120T211830Z-b9dfec`. The Colab runs come from batch `20261120T220024Z-907146` on a Tesla T4, and exactly which run supplies each adapter is listed under [Run selection and provenance](#run-selection-and-provenance). The full generated tables are in [`results-tables.md`](results-tables.md); raw results and every parser output are in [`../results/`](../results/).

## Recommendation

| Input | Use | Fallback | Evidence |
| --- | --- | --- | --- |
| **Complex PDFs**: multi-column, tables without full grid lines, scans | **Docling 2.151.1**, default pipeline, run on the Colab GPU like the other models | If Docling or the Colab runtime is unavailable: PyMuPDF in heuristic mode for pages with a text layer, without claims about reading order or borderless tables. Mark scans "needs OCR" and do not index them. | Docling was the only candidate to recover every table on the table-heavy fixture and to read the two-column page in order, and it had the best structured result on the scan |
| Simple digital PDFs | Docling, as above, for one consistent pipeline | PyMuPDF heuristic mode scores 100 on this fixture and runs locally in 21 ms | Both are accurate here; Docling replaces em dashes with hyphens |
| CSV | **Python `csv`**: BOM, then strict UTF-8, then cp1252; `csv.Sniffer`; every field kept as text | — | 100% exact on both CSVs. Docling also scored 100% but needs its model stack; pandas and Unstructured cannot decode cp1252 |
| XLSX | **openpyxl**, loaded twice: cached values and formulas, plus `sheet_state` and merged ranges | pandas `read_excel` for bulk values only | The only candidate that exposes formulas, merges and the hidden sheet. Docling, Unstructured and pandas recover 100% of values but none of those three |

This answers the specification's open question, Docling versus Unstructured, **for these fixtures**: Docling. Unstructured `hi_res` recovered 77.4% of table cells against Docling's 100%. It read the two-column page at 70.6% order against Docling's 100%. It was 8–41× slower on the three PDFs with a text layer, and only on the scan was it faster (8.6 s against 11.5 s). The evidence is seven small synthetic files, so confirm the choice on real NileTech documents in M3 (see [Limits of this benchmark](#limits-of-this-benchmark)).

## Measured comparison

### PDFs

Scores are 0–100; higher is better. T1–T4 are the four tables of the table-heavy fixture: full grid, borderless, merged two-level header, and rules-only split across a page break.

| Parser | Digital: text / order | Two-column: text / order | Table-heavy: cells (T1 / T2 / T3 / T4), headers | Scan: text / word recall / order / table cells |
| --- | --- | --- | --- | --- |
| **Docling** | 99.5 / 100 | **100 / 100** | **100 (100 / 100 / 100 / 100)**, 77.8 | **97.2** / 66.2 / 91.7 / **80.0** |
| Unstructured `hi_res` | 98.9 / 100 | 51.4 / 70.6 | 77.4 (70 / 0 / 65 / 88), 50.0 | 90.2 / 83.8 / 91.7 / 40.0 |
| Unstructured `ocr_only` | 94.0 / 100 | 96.0 / 94.1 | no table output | 90.2 / 83.8 / 91.7 / no table output |
| Unstructured `fast` | 95.6 / 100 | 53.2 / 76.5 | no table output | 0 (no OCR) |
| PyMuPDF + layout model | **100 / 100** | **100 / 100** | 6.2 (10 / 50 / 15 / 0), 77.8 | 97.1 / **86.5** / 91.7 / 0.0 |
| PyMuPDF, heuristic (laptop) | **100 / 100** | 21.9 / 47.1 | 22.6 (100 / 0 / 100 / 0), 33.3 | 0 (no OCR) |
| pdfplumber (laptop) | 95.6 / 100 | 36.0 / 64.7 | 22.6 (100 / 0 / 100 / 0), 33.3 | 0 |
| Text-strategy table variants (laptop) | same text as above | same | 31.6 (20 / 0 / 0 / 42), 16.7, and 1 spurious table on each prose page | 0 |

On the table-heavy fixture, every candidate keeps 100% of the table values somewhere in its text (98.3% for `ocr_only`). The difference is whether values come back **as table cells in the right row and column**.

### Spreadsheets

| Parser | Tricky UTF-8 CSV (exact fields) | cp1252 `;` CSV | Workbook: values / formulas / hidden sheet / merge kept / sheet names |
| --- | --- | --- | --- |
| **Python `csv`** | **100** | **100** | — |
| Docling | 100 | 100 | 100 / n/a / unknown / n/a / not exposed |
| pandas defaults | 100 | fails: `UnicodeDecodeError` | 100 / n/a / unknown / n/a / yes |
| Unstructured | 96.8 (loses the embedded newline) | fails: `UnicodeDecodeError` | 100 / n/a / unknown / n/a / yes |
| **openpyxl** | — | — | **100 / 100 / flagged / 100 / yes** |

### Timings

All times are medians of warm runs per file. The laptop has an i5-13420H (12 threads) with no GPU and ran 5 warm runs. The Colab runtime has a Tesla T4, 2 vCPUs of an Intel Xeon @ 2.00 GHz and 12.7 GiB RAM, and ran 3 warm runs. *Cold* is the first call, including model loading.

| Parser | Digital (1 page) | Two-column (1 page) | Table-heavy (2 pages) | Scan (1 page) | Cold, first file |
| --- | ---: | ---: | ---: | ---: | ---: |
| Docling | 0.16 s | 0.20 s | 2.35 s | 11.48 s | 15.18 s |
| Unstructured `hi_res` | 6.60 s | 7.07 s | 18.14 s | 8.65 s | 22.72 s |
| Unstructured `ocr_only` | 5.18 s | 6.40 s | 12.47 s | 5.63 s | 5.41 s |
| Unstructured `fast` | 0.05 s | 0.05 s | 0.49 s | 0.00 s | 1.05 s |
| PyMuPDF + layout model | 0.50 s | 0.62 s | 2.86 s | 6.39 s | 0.94 s |
| PyMuPDF heuristic (laptop) | 0.02 s | 0.03 s | 0.06 s | 0.00 s | 0.07 s |

Docling's 11.5 s on the scan is OCR on the CPU. Its log shows RapidOCR falling back with "CUDAExecutionProvider is not in available providers", so the GPU was not used for OCR. CSV and XLSX parsing took under 0.2 s for every candidate.

## Measured limitations

**Docling (the recommendation)**

1. **OCR drops spaces between words.** On the scan, RapidOCR returned `Deliverynoteno.DN-001809`, `within7days` and `Platform2`. Character similarity stays high (97.2), but word recall is 66.2, and one table cell is wrong (`Workflow Automation Platform2`), which is why table cells score 80. Search and citations over OCR text will need whitespace-tolerant matching, or a different OCR engine, which is not measured here.
2. **OCR ran on the CPU** (see Timings). A GPU-enabled ONNX runtime might cut the 11.5 s per scanned page; this is not measured.
3. **Text is normalized.** Em dashes become hyphens (digital word recall 96.9).
4. **Table titles are not headings.** "Table 1. …" and the other titles are not labelled as section headers (table-heavy heading recall 20; on the scan, 0).
5. **Merged headers are flattened.** For T3, the spanning header `Q1 2026` is repeated over each month column (headers 77.8).
6. **Workbooks lose their sheet structure.** Docling returns one grid per detected table with no sheet name (6 grids for 4 sheets), and no formulas, merges or visibility. Its values are complete only once grids are matched by content.
7. **Cold start is 15 s** for the first file while the models load. Docling runs where the other models run, so ingestion of complex PDFs depends on the Colab runtime being up.

**Other candidates**

8. **Unstructured `hi_res`:** found no cells in the borderless T2. It read the two-column page out of order (70.6) and took 18 s for the two-page table file.
9. **Unstructured `ocr_only`:** reads columns well (94.1) but returns no tables and OCRs even pages that have a text layer (5–12 s each).
10. **PyMuPDF + layout model:** perfect reading order, but table cells score 6.2 and it found no table on the scan.
11. **Heuristic PyMuPDF and pdfplumber (laptop):** follow the PDF content stream (two-column order 47.1–64.7), find ruled tables only, and do no OCR. PyMuPDF 2.1 runs an ONNX layout model inside `find_tables()` unless `use_layout=False`. pdfplumber emits `(cid:127)` for the bullet glyph.
12. **Spreadsheets:** only openpyxl exposes formulas, merges and the hidden-sheet flag. Every other workbook parser returns the hidden sheet's data without marking it hidden. Default pandas and Unstructured cannot decode cp1252.

## Run selection and provenance

The comparison uses an **explicit, reviewed selection** in [`../results/selection.json`](../results/selection.json). `bench.report` reads it and maps each adapter to exactly one named run. Selecting the same adapter from two runs is an error, and nothing is chosen because it succeeded. Every result left out appears under *Excluded results* in the tables.

| Run | Adapters used | Pairs | Why |
| --- | --- | --- | --- |
| `20261120T211830Z-b9dfec` (laptop) | the 10 laptop adapters | 28 (26 ok, 2 genuine parser failures) | Committed laptop run |
| `20261120T220551Z-200026` (Colab) | `docling`, `pymupdf_layout` | 11 ok | The notebook's Docling-environment run |
| `20261120T220043Z-890d9e` (Colab) | `unstructured_hi_res`, `unstructured_ocr_only` | 8 ok | A separate invocation in the same session (22:00:43–22:05:49 UTC), which the owner describes as the successful hi_res/ocr_only retry. The notebook's stage logs do not record it, and its environment variables were not saved. |
| `20261120T220748Z-49b6cc` (Colab) | `unstructured_fast`, `unstructured_xlsx` | 5 ok | The notebook's Unstructured-environment run. Its 8 hi_res/ocr_only pairs failed and are **excluded** (below). |

**Evidence kept:**
- the original ZIP, `results/colab-zips/eaios-m1-parsers-20261120T220024Z-907146.zip`, SHA-256 `591fef11…e8685`, unchanged;
- all 44 files, extracted byte-identical into `results/`: the three run files with every parser output, plus the batch folder with `status.json`, `summary.md`, install and run logs, both environment freezes, and the notebook's own results tables;
- the notebook's batch summary, which still reads "16 succeeded, 8 failed". The selection does not rewrite that evidence.

**The later Matplotlib failures.** In run `20261120T220748Z-49b6cc`, all 8 `unstructured_hi_res` and `unstructured_ocr_only` pairs failed with:

`ValueError: Key backend: 'module://matplotlib_inline.backend_inline' is not a valid value for backend`

Colab's kernel exports `MPLBACKEND=module://matplotlib_inline.backend_inline`. The benchmark subprocess inherited it, and the venv's Matplotlib 3.11.2, imported by the Unstructured inference path, rejected it. The notebook now passes `MPLBACKEND=Agg` to every benchmark subprocess (`BENCH_ENV` in cell 1, also used for report generation). `tests/test_colab_status.py` checks this by running cell 1's code under the inline value and confirming that a subprocess sees `Agg`.

**Scores recomputed from saved outputs.** Every committed run was re-scored from its saved parser outputs with the current scorer. Two corrections changed stored scores; both are recorded under `rescored` in the affected file:

* **XLSX merge and visibility scoring (commit `98d57b6`), laptop run:**
  * pandas: merged title yes → n/a, hidden sheet unflagged → unknown;
  * title text added for pandas and openpyxl.
* **Unnamed workbook sheets (this change), Docling run `20261120T220551Z-200026`:**
  * The scorer used to pair unnamed output grids with gold sheets by position. Docling returns one grid per detected table (6 grids for 4 sheets), so every grid after the first was compared with the wrong sheet: cell values 14.1.
  * Unnamed grids are now pooled and matched by content, and sheet identification is reported as n/a. Docling's cell values became 100.0.
  * No other run changed (`python -m bench.rescore <run> --check` reports 0 differences for all four).

**Earlier integrity note.** The first laptop trial runs (discarded, never committed) loaded spaCy's `en_core_web_sm` through Unstructured `partition_xlsx`. That adapter moved to Colab, and the laptop guard now covers spaCy, thinc, NLTK and transformers. Every committed laptop result records `model_modules_loaded: []`.

## Limits of this benchmark

- **Small, synthetic fixture set.** Seven files built with reportlab, Pillow and XlsxWriter, including one simulated scan. There are no Arabic PDFs, no real scans, no forms and no DOCX. The ranking of Docling over Unstructured holds for these fixtures and should be confirmed on real NileTech documents during M3.
- **Gold checked by the agent only.** The implementation agent checked the gold references against rendered pages. **A team member still needs to check them** (see `fixtures/README.md`).
- **Colab runs come from separate invocations.** They share one session, hardware and package set, but the hi_res/ocr_only run was invoked outside the notebook's stage logging, and its environment was not recorded.
- **Different hardware.** Colab timings come from one T4 session with 2 vCPUs and 3 warm runs; laptop timings come from a different machine. Compare candidates within a location, not across locations.
- **Not run.** Amazon Textract and Azure AI Document Intelligence: cloud processing is not permitted.

## Next steps (M3, not part of this PR)

- Ingest complex PDFs through Docling behind the model gateway, with PyMuPDF heuristic mode as the documented fallback and "needs OCR" status for scans when Docling is unavailable.
- Rerun this benchmark on a sample of real NileTech PDFs, and test a GPU ONNX runtime or another OCR engine for the word-spacing problem.
