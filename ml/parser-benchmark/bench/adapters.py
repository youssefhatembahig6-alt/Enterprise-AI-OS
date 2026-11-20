"""Parser adapters: each turns one fixture file into the benchmark's common output shape.

PDF output   {"text": str, "tables": list[grid] | None, "headings": list[str] | None}
CSV output   {"rows": list[list[str]]}
XLSX output  {"sheets": [{"name", "hidden", "grid", "formulas", "merged"}]}

``tables``/``headings`` are ``None`` when the parser has no such output (scored as n/a, not 0).

Each adapter declares where it may run. ``laptop`` adapters use no learned model: the runner
refuses to start them in a process that has imported a model runtime, and checks again after
each call. ``colab`` adapters run learned layout, table or OCR models, or need a package that
pulls a CUDA model stack, and run only in the Colab notebook.

Candidates come from the specification's document-extraction section: Docling, Unstructured
(``fast``, ``hi_res``, ``ocr_only``), PyMuPDF and pdfplumber for PDFs, and a native parser for
CSV/Excel (stdlib ``csv``, pandas, openpyxl). Amazon Textract and Azure AI Document
Intelligence are cloud services and are not run (see the report).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

PDF_CASES = {"digital_pdf", "two_column_pdf", "table_heavy_pdf", "scanned_pdf"}
CSV_CASES = {"csv"}
XLSX_CASES = {"xlsx"}

# Modules whose presence in a laptop run means a learned model could have been executed.
MODEL_MODULES = (
    "onnxruntime",
    "torch",
    "transformers",
    "pymupdf.layout",
    "unstructured_inference",
    "docling",
    "easyocr",
    "rapidocr",
    "spacy",  # Unstructured's partition_xlsx downloads and loads en_core_web_sm at run time
    "thinc",
    "en_core_web_sm",
    "nltk",
)


@dataclass
class Adapter:
    name: str
    label: str
    where: str  # "laptop" | "colab"
    cases: set[str]
    packages: list[str]
    run: Callable[[Path], dict]
    config: str
    uses_models: str = "none"
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


class _HTMLTable(HTMLParser):
    """Minimal HTML table reader (Unstructured's text_as_html, Docling HTML)."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._span = int(dict(attrs).get("colspan") or 1)
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._row.extend([""] * (self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if not self.tables:
                self.tables.append([])
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def html_tables(html: str) -> list[list[list[str]]]:
    p = _HTMLTable()
    p.feed(html)
    return [t for t in p.tables if t]


def _s(v: Any) -> str:
    return "" if v is None else str(v)


# --------------------------------------------------------------------------------------
# laptop PDF adapters (no learned models)
# --------------------------------------------------------------------------------------


def _pymupdf(path: Path, sort: bool, strategy: str = "lines") -> dict:
    import pymupdf

    # PyMuPDF >= 2.0 bundles an ONNX layout model. find_tables() calls it by default
    # (use_layout=True). Disable it so this adapter stays heuristic-only.
    pymupdf._layout_enabled = False
    texts, tables = [], []
    with pymupdf.open(path) as doc:
        for page in doc:
            texts.append(page.get_text("text", sort=sort))
            for t in page.find_tables(use_layout=False, strategy=strategy).tables:
                tables.append([[_s(c) for c in row] for row in t.extract()])
    return {"text": "\n".join(texts), "tables": tables, "headings": None, "pages": len(texts)}


def run_pymupdf(path: Path) -> dict:
    return _pymupdf(path, sort=False)


def run_pymupdf_sorted(path: Path) -> dict:
    return _pymupdf(path, sort=True)


def run_pymupdf_text_tables(path: Path) -> dict:
    return _pymupdf(path, sort=False, strategy="text")


_PLUMBER_TEXT = {"vertical_strategy": "text", "horizontal_strategy": "text"}


def _pdfplumber(path: Path, settings: dict | None) -> dict:
    import pdfplumber

    texts, tables = [], []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            for t in page.extract_tables(settings or {}):
                tables.append([[_s(c) for c in row] for row in t])
    return {"text": "\n".join(texts), "tables": tables, "headings": None, "pages": len(texts)}


def run_pdfplumber(path: Path) -> dict:
    return _pdfplumber(path, None)


def run_pdfplumber_text_tables(path: Path) -> dict:
    return _pdfplumber(path, _PLUMBER_TEXT)


# --------------------------------------------------------------------------------------
# laptop CSV / XLSX adapters
# --------------------------------------------------------------------------------------


def decode_csv_bytes(data: bytes) -> tuple[str, str]:
    """BOM -> utf-8-sig; else strict UTF-8; else cp1252 (never latin-1: it loses the euro sign)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def run_native_csv(path: Path) -> dict:
    text, enc = decode_csv_bytes(path.read_bytes())
    sample = text[:4096]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text, newline=""), dialect))
    return {"rows": rows, "encoding": enc, "delimiter": dialect.delimiter}


def run_pandas_csv(path: Path) -> dict:
    import pandas as pd

    # Defaults a developer would reach for: comma, UTF-8, strings kept as text.
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {"rows": [list(df.columns), *df.astype(str).values.tolist()]}


def _xl_ref(r: int, c: int) -> str:
    s = ""
    c += 1
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return f"{s}{r + 1}"


def run_openpyxl(path: Path) -> dict:
    import openpyxl

    values = openpyxl.load_workbook(path, data_only=True)
    formulas = openpyxl.load_workbook(path, data_only=False)
    sheets = []
    for ws in values.worksheets:
        wf = formulas[ws.title]
        grid = []
        fx = {}
        for r, row in enumerate(ws.iter_rows()):
            out_row = []
            for c, cell in enumerate(row):
                v = cell.value
                if hasattr(v, "isoformat"):
                    v = v.isoformat()
                out_row.append(_s(v))
                fv = wf.cell(row=r + 1, column=c + 1).value
                if isinstance(fv, str) and fv.startswith("="):
                    fx[_xl_ref(r, c)] = fv
            grid.append(out_row)
        sheets.append(
            {
                "name": ws.title,
                "hidden": ws.sheet_state != "visible",
                "grid": grid,
                "formulas": fx,
                "merged": [str(m) for m in ws.merged_cells.ranges],
            }
        )
    return {"sheets": sheets}


def run_pandas_excel(path: Path) -> dict:
    import pandas as pd

    frames = pd.read_excel(path, sheet_name=None, header=None, engine="openpyxl")
    sheets = []
    for name, df in frames.items():
        grid = [["" if (isinstance(v, float) and v != v) else _s(v) for v in row] for row in df.values.tolist()]
        sheets.append({"name": name, "hidden": None, "grid": grid, "formulas": None, "merged": None})
    return {"sheets": sheets}


def _unstructured_tables(elements: list) -> list[list[list[str]]]:
    out = []
    for el in elements:
        html = getattr(el.metadata, "text_as_html", None)
        if html:
            out.extend(html_tables(html))
    return out


def run_unstructured_csv(path: Path) -> dict:
    from unstructured.partition.csv import partition_csv

    els = partition_csv(filename=str(path), include_header=True)
    rows = [r for t in _unstructured_tables(els) for r in t]
    return {"rows": rows}


def run_unstructured_xlsx(path: Path) -> dict:
    from unstructured.partition.xlsx import partition_xlsx

    els = partition_xlsx(filename=str(path), include_header=False)
    by_sheet: dict[str, list[list[str]]] = {}
    for el in els:
        name = getattr(el.metadata, "page_name", None) or "?"
        html = getattr(el.metadata, "text_as_html", None)
        grid = by_sheet.setdefault(name, [])
        if html:
            for t in html_tables(html):
                grid.extend(t)
        elif el.text:
            grid.append([el.text])
    return {
        "sheets": [
            {"name": n, "hidden": None, "grid": g, "formulas": None, "merged": None} for n, g in by_sheet.items()
        ]
    }


# --------------------------------------------------------------------------------------
# Colab adapters (learned layout / table / OCR models)
# --------------------------------------------------------------------------------------

_DOCLING_CONVERTER: Any = None


def _docling_converter() -> Any:
    global _DOCLING_CONVERTER
    if _DOCLING_CONVERTER is None:
        from docling.document_converter import DocumentConverter

        _DOCLING_CONVERTER = DocumentConverter()  # default pipeline: layout, TableFormer, OCR
    return _DOCLING_CONVERTER


def _docling_doc(path: Path) -> Any:
    return _docling_converter().convert(str(path)).document


def run_docling_pdf(path: Path) -> dict:
    doc = _docling_doc(path)
    tables = []
    for t in doc.tables:
        df = t.export_to_dataframe(doc=doc)
        tables.append([[_s(c) for c in df.columns], *[[_s(v) for v in row] for row in df.values.tolist()]])
    headings = [
        it.text
        for it in doc.texts
        if str(getattr(it, "label", "")).split(".")[-1].lower() in ("title", "section_header")
    ]
    return {"text": doc.export_to_markdown(), "tables": tables, "headings": headings}


def run_docling_csv(path: Path) -> dict:
    doc = _docling_doc(path)
    rows: list[list[str]] = []
    for t in doc.tables:
        df = t.export_to_dataframe(doc=doc)
        rows.extend([[_s(c) for c in df.columns], *[[_s(v) for v in r] for r in df.values.tolist()]])
    return {"rows": rows}


def run_docling_xlsx(path: Path) -> dict:
    doc = _docling_doc(path)
    sheets = []
    for i, t in enumerate(doc.tables):
        df = t.export_to_dataframe(doc=doc)
        grid = [[_s(c) for c in df.columns], *[[_s(v) for v in r] for r in df.values.tolist()]]
        sheets.append({"name": None, "hidden": None, "grid": grid, "formulas": None, "merged": None, "index": i})
    # Docling may return several tables per sheet and no sheet names: merge all into one
    # unnamed sheet per table; the scorer then matches sheets by order.
    return {"sheets": sheets}


def _unstructured_pdf(path: Path, strategy: str) -> dict:
    from unstructured.partition.pdf import partition_pdf

    kwargs: dict[str, Any] = {"filename": str(path), "strategy": strategy}
    if strategy == "hi_res":
        kwargs["infer_table_structure"] = True
    els = partition_pdf(**kwargs)
    text = "\n\n".join(el.text for el in els if el.text)
    tables = _unstructured_tables(els) if strategy == "hi_res" else None
    headings = [el.text for el in els if getattr(el, "category", "") == "Title"]
    return {"text": text, "tables": tables, "headings": headings}


def run_unstructured_fast(path: Path) -> dict:
    return _unstructured_pdf(path, "fast")


def run_unstructured_hi_res(path: Path) -> dict:
    return _unstructured_pdf(path, "hi_res")


def run_unstructured_ocr_only(path: Path) -> dict:
    return _unstructured_pdf(path, "ocr_only")


def run_pymupdf_layout(path: Path) -> dict:
    import pymupdf
    import pymupdf.layout
    import pymupdf4llm

    md = pymupdf4llm.to_markdown(str(path))
    tables = []
    with pymupdf.open(path) as doc:
        for page in doc:
            for t in page.find_tables().tables:  # use_layout=True (default)
                tables.append([[_s(c) for c in row] for row in t.extract()])
    return {"text": md, "tables": tables, "headings": None}


# --------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------

ADAPTERS: dict[str, Adapter] = {
    a.name: a
    for a in [
        Adapter(
            "pymupdf",
            "PyMuPDF (no layout model)",
            "laptop",
            PDF_CASES,
            ["pymupdf"],
            run_pymupdf,
            "page.get_text('text'); page.find_tables(use_layout=False); pymupdf._layout_enabled=False",
            notes="Spec's digital-PDF fallback. Text in content-stream order.",
        ),
        Adapter(
            "pymupdf_sorted",
            "PyMuPDF sort=True (no layout model)",
            "laptop",
            PDF_CASES,
            ["pymupdf"],
            run_pymupdf_sorted,
            "page.get_text('text', sort=True); find_tables(use_layout=False)",
            notes="Same parser, geometric top-to-bottom sort.",
        ),
        Adapter(
            "pdfplumber",
            "pdfplumber",
            "laptop",
            PDF_CASES,
            ["pdfplumber", "pdfminer.six"],
            run_pdfplumber,
            "page.extract_text(); page.extract_tables() with default settings",
            notes="Spec's targeted table/geometry tool.",
        ),
        Adapter(
            "pymupdf_text_tables",
            "PyMuPDF, text-strategy tables",
            "laptop",
            PDF_CASES,
            ["pymupdf"],
            run_pymupdf_text_tables,
            "get_text('text'); find_tables(use_layout=False, strategy='text')",
            notes="Table finder variant for borderless tables.",
        ),
        Adapter(
            "pdfplumber_text_tables",
            "pdfplumber, text-strategy tables",
            "laptop",
            PDF_CASES,
            ["pdfplumber", "pdfminer.six"],
            run_pdfplumber_text_tables,
            "extract_tables({'vertical_strategy': 'text', 'horizontal_strategy': 'text'})",
            notes="Table finder variant for borderless tables.",
        ),
        Adapter(
            "native_csv",
            "Python csv (native)",
            "laptop",
            CSV_CASES,
            [],
            run_native_csv,
            "BOM/UTF-8/cp1252 decode; csv.Sniffer over , ; tab |; csv.reader",
            notes="Spec's native CSV parser.",
        ),
        Adapter(
            "pandas_csv",
            "pandas.read_csv (defaults)",
            "laptop",
            CSV_CASES,
            ["pandas"],
            run_pandas_csv,
            "read_csv(dtype=str, keep_default_na=False); default sep and encoding",
        ),
        Adapter(
            "unstructured_csv",
            "Unstructured partition_csv",
            "laptop",
            CSV_CASES,
            ["unstructured"],
            run_unstructured_csv,
            "partition_csv(include_header=True); rows from text_as_html",
        ),
        Adapter(
            "openpyxl",
            "openpyxl (values + formulas)",
            "laptop",
            XLSX_CASES,
            ["openpyxl"],
            run_openpyxl,
            "load_workbook twice: data_only=True (cached values) and data_only=False (formulas)",
            notes="Spec's native Excel parser.",
        ),
        Adapter(
            "pandas_excel",
            "pandas.read_excel",
            "laptop",
            XLSX_CASES,
            ["pandas", "openpyxl"],
            run_pandas_excel,
            "read_excel(sheet_name=None, header=None, engine='openpyxl')",
        ),
        # ---- Colab only ----
        Adapter(
            "unstructured_xlsx",
            "Unstructured partition_xlsx",
            "colab",
            XLSX_CASES,
            ["unstructured"],
            run_unstructured_xlsx,
            "partition_xlsx(include_header=False); rows from text_as_html, grouped by page_name",
            uses_models="spaCy en_core_web_sm (partition_xlsx downloads and loads it at run time)",
            notes="Moved off the laptop after a trial run showed it loading the spaCy model.",
        ),
        Adapter(
            "docling",
            "Docling (default pipeline)",
            "colab",
            PDF_CASES | CSV_CASES | XLSX_CASES,
            ["docling", "docling-core"],
            lambda p: (
                run_docling_csv(p)
                if p.suffix == ".csv"
                else run_docling_xlsx(p)
                if p.suffix == ".xlsx"
                else run_docling_pdf(p)
            ),
            "DocumentConverter() defaults; export_to_markdown; tables via export_to_dataframe",
            uses_models="layout model, TableFormer, OCR engine (PDF); none for CSV/XLSX",
        ),
        Adapter(
            "unstructured_fast",
            "Unstructured fast",
            "colab",
            PDF_CASES,
            ["unstructured", "unstructured-inference"],
            run_unstructured_fast,
            "partition_pdf(strategy='fast')",
            uses_models="none at run time; the package import requires unstructured-inference (CUDA torch stack)",
        ),
        Adapter(
            "unstructured_hi_res",
            "Unstructured hi_res",
            "colab",
            PDF_CASES,
            ["unstructured", "unstructured-inference"],
            run_unstructured_hi_res,
            "partition_pdf(strategy='hi_res', infer_table_structure=True)",
            uses_models="layout detection model, table-structure model, Tesseract OCR",
        ),
        Adapter(
            "unstructured_ocr_only",
            "Unstructured ocr_only",
            "colab",
            PDF_CASES,
            ["unstructured", "unstructured-inference"],
            run_unstructured_ocr_only,
            "partition_pdf(strategy='ocr_only')",
            uses_models="Tesseract OCR",
        ),
        Adapter(
            "pymupdf_layout",
            "PyMuPDF + layout model",
            "colab",
            PDF_CASES,
            ["pymupdf", "pymupdf-layout", "pymupdf4llm"],
            run_pymupdf_layout,
            "pymupdf4llm.to_markdown with pymupdf.layout active; find_tables(use_layout=True)",
            uses_models="PyMuPDF ONNX layout model",
            notes="Not named in the spec; included because PyMuPDF >= 2.0 enables it by default.",
        ),
    ]
}

CLOUD_CANDIDATES = {
    "Amazon Textract": "Cloud OCR/forms service. Spec: only where cloud processing is permitted. Not run: no permission, no account, and the project keeps documents local.",
    "Azure AI Document Intelligence": "Same position as Textract. Not run.",
}
