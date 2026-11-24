"""Canonical intermediate representation of a parsed document (specification section 9).

Every parser returns typed elements in reading order. ``build_canonical`` joins them
into one canonical text (NFC, ``\\n`` line endings, elements separated by a blank
line) and records each element's code-point span in that text. Citations, chunk
offsets and highlights all refer to code points of this exact text, identified by
its SHA-256, so a citation always reopens the version it was made from.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

ElementType = Literal["heading", "paragraph", "list_item", "table", "code"]
SEPARATOR = "\n\n"


@dataclass(frozen=True, slots=True)
class Element:
    element_type: ElementType
    text: str
    page: int | None = None
    heading_path: tuple[str, ...] = ()
    # For tables: index (in code points, relative to ``text``) where the header row
    # ends, so split table chunks can repeat the header.
    header_end: int | None = None


@dataclass(frozen=True, slots=True)
class PlacedElement:
    element: Element
    start: int  # inclusive, code points into the canonical text
    end: int  # exclusive


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    text: str
    text_sha256: str
    elements: tuple[PlacedElement, ...]
    parser: str
    notes: dict[str, object] = field(default_factory=dict)


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return unicodedata.normalize("NFC", value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_canonical(
    elements: list[Element], parser: str, notes: dict[str, object] | None = None
) -> CanonicalDocument:
    parts: list[str] = []
    placed: list[PlacedElement] = []
    cursor = 0
    for element in elements:
        text = normalize_text(element.text).strip("\n")
        if not text.strip():
            continue
        if parts:
            parts.append(SEPARATOR)
            cursor += len(SEPARATOR)
        normalized = Element(
            element.element_type, text, element.page, element.heading_path, element.header_end
        )
        placed.append(PlacedElement(normalized, cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    canonical = "".join(parts)
    return CanonicalDocument(
        text=canonical,
        text_sha256=sha256_text(canonical),
        elements=tuple(placed),
        parser=parser,
        notes=dict(notes or {}),
    )


def line_range(text: str, first_line: int, start: int, end: int) -> tuple[int, int]:
    """1-based inclusive source lines covered by the code-point span ``[start, end)`` of a
    verbatim (code) canonical text whose first line is source line ``first_line``."""

    last = max(start, end - 1)
    return first_line + text.count("\n", 0, start), first_line + text.count("\n", 0, last)


def elements_to_json(doc: CanonicalDocument) -> list[dict[str, Any]]:
    """Element spans as stored with the version (``document_versions.elements``)."""

    return [
        {
            "type": placed.element.element_type,
            "start": placed.start,
            "end": placed.end,
            "page": placed.element.page,
            "heading_path": list(placed.element.heading_path),
            "header_end": placed.element.header_end,
        }
        for placed in doc.elements
    ]


def from_stored(
    text: str,
    text_sha256: str,
    stored: list[dict[str, Any]],
    parser: str,
    notes: dict[str, object] | None = None,
) -> CanonicalDocument:
    """Rebuild a canonical document from stored text and element spans. The text hash
    is verified, so a rebuilt document is exactly the one that was cited."""

    if sha256_text(text) != text_sha256:
        raise ValueError("stored canonical text does not match its SHA-256")
    placed = []
    for item in stored:
        start, end = int(item["start"]), int(item["end"])
        element = Element(
            element_type=item["type"],
            text=text[start:end],
            page=item.get("page"),
            heading_path=tuple(item.get("heading_path") or ()),
            header_end=item.get("header_end"),
        )
        placed.append(PlacedElement(element, start, end))
    return CanonicalDocument(text, text_sha256, tuple(placed), parser, dict(notes or {}))
