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
from typing import Literal

ElementType = Literal["heading", "paragraph", "list_item", "table"]
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
