# Colab batch `20261120T220024Z-907146`

24 expected pairs: 16 succeeded, 8 failed, 0 missing.

## Stages

| Environment | Stage | Result | Exit | Detail |
| --- | --- | --- | --- | --- |
| docling | install | ok | 0 | $ cd /content/eaios/ml/parser-benchmark && /content/venv-docling/bin/python -c 'import docling.document_converter, pymup |
| docling | run | ok | 0 | wrote results/20261120T220551Z-200026.json |
| unstructured | install | ok | 0 |  Found libcurl/8.5.0 OpenSSL/3.0.13 zlib/1.3 brotli/1.1.0 zstd/1.5.5 libidn2/2.3.7 libpsl/0.21.2 (+libidn2/2.3.7) libssh |
| unstructured | run | ok | 0 | wrote results/20261120T220748Z-49b6cc.json |
| — | report | ok | 0 | |

## Pairs

| Environment | Adapter | Fixture | State | Failed stage | Detail |
| --- | --- | --- | --- | --- | --- |
| docling | docling | digital-company-overview | succeeded |  |  |
| docling | docling | two-column-newsletter | succeeded |  |  |
| docling | docling | table-heavy-sales-q1-2026 | succeeded |  |  |
| docling | docling | scanned-delivery-note | succeeded |  |  |
| docling | docling | tricky-customers-csv | succeeded |  |  |
| docling | docling | semicolon-cp1252-csv | succeeded |  |  |
| docling | docling | multi-sheet-workbook | succeeded |  |  |
| docling | pymupdf_layout | digital-company-overview | succeeded |  |  |
| docling | pymupdf_layout | two-column-newsletter | succeeded |  |  |
| docling | pymupdf_layout | table-heavy-sales-q1-2026 | succeeded |  |  |
| docling | pymupdf_layout | scanned-delivery-note | succeeded |  |  |
| unstructured | unstructured_fast | digital-company-overview | succeeded |  |  |
| unstructured | unstructured_fast | two-column-newsletter | succeeded |  |  |
| unstructured | unstructured_fast | table-heavy-sales-q1-2026 | succeeded |  |  |
| unstructured | unstructured_fast | scanned-delivery-note | succeeded |  |  |
| unstructured | unstructured_hi_res | digital-company-overview | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_hi_res | two-column-newsletter | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_hi_res | table-heavy-sales-q1-2026 | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_hi_res | scanned-delivery-note | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_ocr_only | digital-company-overview | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_ocr_only | two-column-newsletter | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_ocr_only | table-heavy-sales-q1-2026 | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_ocr_only | scanned-delivery-note | failed | parse | kend_inline' is not a valid value for backend; supported values are ['gtk3agg', 'gtk3cairo', 'gtk4agg', 'gtk4cairo', 'ma |
| unstructured | unstructured_xlsx | multi-sheet-workbook | succeeded |  |  |
