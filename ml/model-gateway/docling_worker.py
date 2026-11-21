"""Docling worker for the gateway's ``POST /parse`` route (M3).

Runs inside the M1 Docling environment (``ml/parser-benchmark/colab-docling-pins.txt``,
installed in its own venv because Docling's pins conflict with vLLM's), one process
per document:

    $DOCLING_PYTHON docling_worker.py /path/to/file.pdf  >  elements JSON on stdout

The default Docling pipeline is the one M1 measured (layout model, TableFormer, OCR on
pages without a text layer). Output is the backend's element contract
(``app.documents.parsers.elements_from_docling``): elements in Docling's reading order,
page furniture (running headers, footers, page numbers) dropped, tables as rows joined
with `` | `` and the header row's end recorded so split table chunks repeat it.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from typing import Any

HEADING_LABELS = {"title", "section_header"}
LIST_LABELS = {"list_item"}
TEXT_LABELS = {"text", "paragraph", "caption", "footnote", "formula", "code", "reference"}
FURNITURE_LABELS = {"page_header", "page_footer"}


def _label(item: Any) -> str:
    return str(getattr(item, "label", "")).split(".")[-1].lower()


def _page(item: Any) -> int | None:
    prov = getattr(item, "prov", None) or []
    return int(prov[0].page_no) if prov else None


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("|", "/").split())


def _converter() -> Any:
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    except ImportError:  # older layout
        from docling.datamodel.pipeline_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # Default pipeline (as measured in M1). The device is configurable because the
    # gateway shares the T4 with vLLM at 0.75 utilisation; CPU is the safe default.
    device = os.environ.get("DOCLING_DEVICE", "cpu").upper()
    options = PdfPipelineOptions()
    options.accelerator_options = AcceleratorOptions(device=getattr(AcceleratorDevice, device))
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def convert(path: str) -> dict[str, Any]:
    doc = _converter().convert(path).document
    elements: list[dict[str, Any]] = []
    path_stack: list[tuple[int, str]] = []
    for item, _level in doc.iterate_items():
        label = _label(item)
        if label in FURNITURE_LABELS:
            continue
        page = _page(item)
        if label in HEADING_LABELS:
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            level = 0 if label == "title" else int(getattr(item, "level", 1) or 1)
            while path_stack and path_stack[-1][0] >= level:
                path_stack.pop()
            path_stack.append((level, text))
            elements.append({"type": "heading", "text": text, "page": page,
                             "heading_path": [t for _, t in path_stack]})
        elif label == "table":
            frame = item.export_to_dataframe(doc=doc)
            header = " | ".join(_cell(c) for c in frame.columns)
            rows = [" | ".join(_cell(v) for v in row) for row in frame.values.tolist()]
            body = "\n".join([header, *rows]) if header.strip(" |") else "\n".join(rows)
            if not body.strip():
                continue
            elements.append({"type": "table", "text": body, "page": page,
                             "heading_path": [t for _, t in path_stack],
                             "header_end": len(header) if header.strip(" |") else None})
        elif label in LIST_LABELS or label in TEXT_LABELS:
            text = (getattr(item, "text", "") or "").strip()
            if text:
                elements.append({"type": "list_item" if label in LIST_LABELS else "paragraph",
                                 "text": text, "page": page,
                                 "heading_path": [t for _, t in path_stack]})
    pages = len(getattr(doc, "pages", {}) or {})
    version = metadata.version("docling")
    return {
        "parser": f"docling-{version}",
        "elements": elements,
        "notes": {
            "docling_version": version,
            "docling_core_version": metadata.version("docling-core"),
            "pipeline": "default (layout, TableFormer, OCR)",
            "device": os.environ.get("DOCLING_DEVICE", "cpu"),
            "pages": pages,
        },
    }


if __name__ == "__main__":
    json.dump(convert(sys.argv[1]), sys.stdout, ensure_ascii=False)
