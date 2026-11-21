# ADR 0302 — Parsing and chunking for M3

Status: accepted for M3, pending planning-agent review. Source: M1 parser comparison (`ml/parser-benchmark/reports/2026-11-20-parser-comparison.md`), specification §9 "Document extraction" and "Chunking strategies". Implementation: `backend/app/documents/parsers.py`, `canonical.py`, `chunker.py`.

## Decision

- **M1's recommendation is Docling for complex PDFs, run on the Colab GPU**, with PyMuPDF heuristic text as the fallback when Docling or Colab is unavailable. M3 implements that **fallback path locally**, using **pdfplumber** (also measured in M1; digital text 95.6, ruled tables only) rather than PyMuPDF. PyMuPDF 2.1 hard-depends on `onnxruntime` and a layout model, which would put a model runtime in the backend image against the no-models-on-laptops rule, and it is AGPL-licensed. pdfplumber makes no claims about borderless tables or multi-column reading order (M1 measured 64.7 order on the two-column page). PDFs without a text layer are `needs_ocr` and are not indexed.
- CSV uses Python `csv` (BOM, strict UTF-8, then cp1252; Sniffer; fields as text) and XLSX uses openpyxl cached values, exactly as M1 recommends. Hidden sheets are **not indexed** and are listed in the parse notes; formulas are not indexed. Markdown and plain text use a built-in parser.
- Every parser emits typed elements, joined into one **canonical text** (NFC, `\n`, blank line between elements) with each element's code-point span.
- **Chunking** is deterministic, header-aware and recursive: paragraphs under one heading path are packed up to 700 code points; longer elements split at sentence ends, then whitespace; tables stay whole or split into row groups that repeat the header in a `prefix`. A chunk's citable span is contiguous canonical text; the prefix (section path, repeated header) is context only.

## Consequences and open items

- **Docling is not integrated** in M3. Wiring it means a gateway `/parse` route returning canonical elements, run in the Colab runtime, with the same canonical-text contract. Until then, complex PDFs are indexed with the fallback's known limits. Unresolved acceptance item.
- 700 code points is a heuristic for the specification's 350-token budget. Exact checker token counts are enforced by the gateway, which refuses pairs over 512 tokens instead of truncating.
- The M1 fixtures are synthetic; the parser choice still needs confirming on real NileTech documents.
