# Parser benchmark fixtures

All seven files are **synthetic** and safe for this public repository. [`generate.py`](generate.py) writes them, together with their gold references and [`manifest.json`](manifest.json), which records each file's SHA-256, size, source and notes.

| Source label | Content | Provenance |
| --- | --- | --- |
| `baseline-public` | `about.md`, verbatim | From the PUBLIC document in `niletech-0.1.6.zip`. Its SHA-256 matches `backend/fixtures/business/niletech-0.1.6/source-manifest.json` (checked by `tests/test_fixtures.py`). |
| `baseline-sales` | Q1 2026 aggregates, one order (SO-001809) and customer names | From the vendored sales tables, aggregated with `Decimal`. The generator asserts the frozen totals: 80,934,385.00 net, 291 orders, 11,330,813.90 tax, plus the pending and cancelled values. |
| `synthetic` | Every other sentence, CSV row and workbook note | Written for the benchmark. Not a NileTech publication. Each PDF carries the line "Synthetic benchmark fixture. Not a NileTech publication." |

INTERNAL, CONFIDENTIAL and RESTRICTED baseline documents (policies, contracts, payroll) are deliberately **not** used. The baseline itself has no PDFs or spreadsheets, only Markdown and JSONL, so none of the seven benchmark cases could reuse a baseline file directly.

## Regenerate and verify

```bash
cd ml/parser-benchmark
.venv/bin/python fixtures/generate.py          # rewrites files/, gold/, manifest.json
.venv/bin/python fixtures/generate.py --check  # regenerates in a temp dir; must print "fixtures reproduce byte-for-byte"
```

Byte-identical output requires the pinned `reportlab`, `pillow` and `xlsxwriter` from `requirements-laptop.txt`, and the DejaVu fonts whose hashes are in the manifest (Debian/Ubuntu package `fonts-dejavu-core`). Reportlab runs in invariant mode, Pillow's PDF dates are pinned, and XlsxWriter's creation date is fixed.

## Gold format

* **PDF**: `text` (full text in reading order, with table rows as space-joined cells), `reading_order` (paragraph-level anchors, in order), `tables` (`header_rows` and `rows`, cell strings exactly as printed), `headings`, `furniture` (page footers, which are ignored when scoring), and `layout` notes.
* **CSV**: `rows` (every field exactly, with no trimming), plus the named `checks`.
* **XLSX**: one entry per sheet with `hidden`, `merged` and `cells` (`A1 → {value, display, type, formula}`). `value` is the cached value. `display` is the formatted value, for example `33.0%`.

## How the gold was hand-checked

Each PDF page was rendered with `pdftoppm -r 80 -png` and compared with its gold file by eye. The check covered these points:

1. Every gold text block is on the page, in the order a reader would follow. For the two-column page: left column, then right column, then the full-width note. The paragraph "Query volume tripled…" continues at the top of the right column with "and refreshed incrementally during the day.", which is recorded as an extra anchor.
2. Every table cell in the gold file matches the printed cell. T4 prints rows NIL-0001 to NIL-0012 on page 1, and its header repeats above NIL-0013 on page 2.
3. The scan is legible. The header cells "Unit price (USD)" and "Line total (USD)" fit inside their borders; an earlier version overflowed and was corrected.
4. The CSVs were checked as raw bytes (`od -c` shows the BOM and CRLF; `iconv -f cp1252` decodes the euro sign). The workbook was read back with openpyxl, and every cached value and formula matched the gold file (the openpyxl row in the results).

The implementation agent did this check on 2026-11-20. **A team member should repeat items 1–3 before the gold is treated as accepted.**
