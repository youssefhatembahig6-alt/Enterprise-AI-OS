"""Format detection and parsing into canonical elements (M1 parser decision, ADR 0302).

* Markdown / plain text: built-in; headings (``#``), paragraphs, list items, pipe tables.
* CSV: Python ``csv``; BOM, strict UTF-8, then cp1252; ``csv.Sniffer``; every field kept
  as text (M1: 100% exact).
* XLSX: openpyxl cached values; one table per *visible* sheet; hidden sheets are not
  indexed and are listed in the parse notes.
* PDF with a text layer: pdfplumber page text. This is M1's local fallback for when
  Docling/Colab is unavailable: no claims about borderless tables or multi-column
  reading order.
* PDF without a text layer (a scan): ``needs_ocr``, not indexed (M1 recommendation).

Docling (the M1 choice for complex PDFs) runs on the Colab gateway; when the
gateway's ``/parse`` route is configured, ``app.documents.ingest`` uses it for PDFs
and falls back to this module otherwise. No model runs here.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.documents.canonical import CanonicalDocument, Element, build_canonical

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_XLSX_CELLS = 200_000

SUPPORTED = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
}


class ParseFailed(Exception):
    """The file cannot be indexed. ``code`` and ``message`` are shown to the uploader."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class NeedsOcr(ParseFailed):
    def __init__(self) -> None:
        super().__init__(
            "needs_ocr",
            "This PDF has no text layer (it looks scanned). Scans need OCR, which is not "
            "available yet, so it was not indexed.",
        )


@dataclass(frozen=True, slots=True)
class Detected:
    extension: str
    media_type: str


def detect(filename: str, data: bytes) -> Detected:
    """Decide the format from the extension and confirm it with the file's signature."""

    match = re.search(r"(\.[A-Za-z0-9]+)$", filename or "")
    extension = match.group(1).lower() if match else ""
    if extension not in SUPPORTED:
        raise ParseFailed(
            "unsupported_type",
            "Unsupported file type. Upload PDF, Markdown, plain text, CSV or XLSX.",
        )
    if extension == ".pdf" and not data.startswith(b"%PDF-"):
        raise ParseFailed("malformed_file", "The file is not a valid PDF.")
    if extension == ".xlsx" and not data.startswith(b"PK"):
        raise ParseFailed("malformed_file", "The file is not a valid XLSX workbook.")
    return Detected(extension, SUPPORTED[extension])


def _decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def parse_markdown(data: bytes) -> CanonicalDocument:
    try:
        text, encoding = _decode_text(data)
    except UnicodeDecodeError as exc:
        raise ParseFailed("encoding_error", "The text could not be decoded.") from exc
    elements: list[Element] = []
    path: list[tuple[int, str]] = []
    block: list[str] = []

    def heading_path() -> tuple[str, ...]:
        return tuple(title for _, title in path)

    def flush() -> None:
        if not block:
            return
        lines = list(block)
        block.clear()
        if all(line.lstrip().startswith("|") for line in lines):
            rows = [ln for ln in lines if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", ln)]
            table = "\n".join(r.strip() for r in rows)
            header_end = len(rows[0].strip()) if rows else None
            elements.append(Element("table", table, None, heading_path(), header_end))
        elif all(_LIST.match(line) for line in lines):
            for line in lines:
                elements.append(Element("list_item", line.strip(), None, heading_path()))
        else:
            elements.append(Element("paragraph", "\n".join(lines), None, heading_path()))

    for raw in text.replace("\r\n", "\n").split("\n"):
        heading = _HEADING.match(raw)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2)
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, title))
            elements.append(Element("heading", title, None, heading_path()))
        elif not raw.strip():
            flush()
        else:
            block.append(raw.rstrip())
    flush()
    if not elements:
        raise ParseFailed("empty_document", "The document contains no text.")
    return build_canonical(elements, "builtin-text-v1", {"encoding": encoding})


def _table_text(rows: list[list[str]]) -> tuple[str, int | None]:
    lines = [
        " | ".join(cell.strip() for cell in row) for row in rows if any(c.strip() for c in row)
    ]
    if not lines:
        return "", None
    return "\n".join(lines), len(lines[0])


def parse_csv(data: bytes) -> CanonicalDocument:
    try:
        text, encoding = _decode_text(data)
    except UnicodeDecodeError as exc:
        raise ParseFailed("encoding_error", "The CSV could not be decoded.") from exc
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), dialect))
    except csv.Error as exc:
        raise ParseFailed("malformed_file", "The CSV could not be parsed.") from exc
    table, header_end = _table_text(rows)
    if not table:
        raise ParseFailed("empty_document", "The CSV contains no rows.")
    return build_canonical(
        [Element("table", table, None, (), header_end)],
        "python-csv-v1",
        {"encoding": encoding, "delimiter": getattr(dialect, "delimiter", ",")},
    )


def parse_xlsx(data: bytes) -> CanonicalDocument:
    from openpyxl import load_workbook  # noqa: PLC0415 - heavy import, only for XLSX

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=False, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ParseFailed("malformed_file", "The workbook could not be opened.") from exc
    elements: list[Element] = []
    hidden: list[str] = []
    cells = 0
    for sheet in workbook.worksheets:
        if sheet.sheet_state != "visible":
            hidden.append(sheet.title)
            continue
        rows: list[list[str]] = []
        for row in sheet.iter_rows(values_only=True):
            cells += len(row)
            if cells > MAX_XLSX_CELLS:
                raise ParseFailed("too_large", "The workbook has too many cells to index.")
            rows.append(["" if value is None else str(value) for value in row])
        table, header_end = _table_text(rows)
        if table:
            elements.append(Element("heading", sheet.title, None, (sheet.title,)))
            elements.append(Element("table", table, None, (sheet.title,), header_end))
    if not elements:
        raise ParseFailed("empty_document", "The workbook has no visible data.")
    return build_canonical(elements, "openpyxl-values-v1", {"hidden_sheets_not_indexed": hidden})


def parse_pdf(data: bytes) -> CanonicalDocument:
    import pdfplumber  # noqa: PLC0415 - heavy import, only for PDFs
    from pdfminer.pdfparser import PDFSyntaxError  # noqa: PLC0415

    elements: list[Element] = []
    empty_pages: list[int] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if not pdf.pages:
                raise ParseFailed("empty_document", "The PDF has no pages.")
            for number, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").replace("(cid:127)", "•")
                paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
                if not paragraphs:
                    empty_pages.append(number)
                for paragraph in paragraphs:
                    elements.append(Element("paragraph", paragraph, number, ()))
    except ParseFailed:
        raise
    except (PDFSyntaxError, ValueError, KeyError, TypeError) as exc:
        raise ParseFailed("malformed_file", "The PDF could not be read.") from exc
    except Exception as exc:  # pdfminer raises many private exception types
        raise ParseFailed("malformed_file", "The PDF could not be read.") from exc
    if not elements:
        raise NeedsOcr
    return build_canonical(
        elements, "pdfplumber-text-v1", {"pages_without_text_layer": empty_pages}
    )


class DoclingUnavailable(Exception):
    """Docling could not run (gateway down, not configured, timed out, failed)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


DoclingCall = Callable[[str, bytes], dict[str, Any]]
ELEMENT_TYPES = ("heading", "paragraph", "list_item", "table")


def elements_from_docling(payload: dict[str, Any]) -> CanonicalDocument:
    """Validate the gateway's Docling element list and build the canonical document.

    The gateway converts Docling's reading-order items to the same element contract the
    local parsers use (headings, paragraphs, list items, tables as ``a | b`` rows with
    the header row first), so chunking and citation offsets work identically.
    """

    raw = payload.get("elements")
    parser = payload.get("parser")
    if not isinstance(raw, list) or not isinstance(parser, str) or not parser.startswith("docling"):
        raise DoclingUnavailable("malformed_parse_result")
    elements: list[Element] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") not in ELEMENT_TYPES:
            raise DoclingUnavailable("malformed_parse_result")
        text_value = item.get("text")
        page = item.get("page")
        path = item.get("heading_path") or []
        header_end = item.get("header_end")
        if not isinstance(text_value, str) or not isinstance(path, list):
            raise DoclingUnavailable("malformed_parse_result")
        elements.append(
            Element(
                item["type"],
                text_value,
                page if isinstance(page, int) else None,
                tuple(str(p) for p in path),
                header_end if isinstance(header_end, int) else None,
            )
        )
    notes = dict(payload.get("notes") or {})
    doc = build_canonical(elements, parser, notes)
    if not doc.text.strip():
        if notes.get("ocr_pages"):
            raise NeedsOcr
        raise ParseFailed("empty_document", "Docling found no text in the PDF.")
    return doc


def parse(filename: str, data: bytes, docling: DoclingCall | None = None) -> CanonicalDocument:
    """Parse an upload. PDFs go to Docling when a Docling call is available (the M1
    choice: layout, tables, OCR on the Colab GPU); if Docling is unavailable, the
    documented fallback is the local text-layer parser, and scans become needs_ocr."""

    if len(data) > MAX_UPLOAD_BYTES:
        raise ParseFailed("too_large", "The file is larger than 10 MB.")
    if not data:
        raise ParseFailed("empty_document", "The file is empty.")
    detected = detect(filename, data)
    if detected.extension in (".md", ".markdown", ".txt"):
        return parse_markdown(data)
    if detected.extension == ".csv":
        return parse_csv(data)
    if detected.extension == ".xlsx":
        return parse_xlsx(data)
    unavailable: str | None = None
    if docling is not None:
        try:
            return elements_from_docling(docling(filename, data))
        except DoclingUnavailable as exc:
            unavailable = exc.code
    doc = parse_pdf(data)
    if unavailable is not None:
        doc.notes["docling"] = f"unavailable: {unavailable}"
        doc.notes["fallback"] = "pdfplumber text layer (M1 documented fallback)"
    return doc
