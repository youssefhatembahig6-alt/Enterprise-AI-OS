"""Deterministic, header-aware recursive chunking (specification section 9).

* Consecutive paragraphs and list items under the same heading path (and page) are
  packed into one chunk up to ``MAX_CHARS`` code points.
* Longer elements split at sentence boundaries, then at whitespace; never mid-word
  unless a single word exceeds the limit.
* Tables stay whole when they fit; otherwise they split into row groups and each
  later group repeats the header row in its ``prefix``.
* ``span`` (``char_start``..``char_end``) is a contiguous code-point range of the
  canonical text. ``text`` = ``prefix`` + that span; the prefix (section path, repeated
  table header) is context for embedding and the model, never part of a citation.

``MAX_CHARS`` = 700 code points keeps a chunk below the specification's 350-token
budget for typical English text. Exact checker token counts are enforced by the
gateway, which refuses pairs over 512 tokens instead of truncating.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.documents.canonical import CanonicalDocument, PlacedElement

CHUNKER_VERSION = "header-recursive-v1"
CODE_CHUNKER_VERSION = "header-recursive-v1+code-lines-v1"
MAX_CHARS = 700
_SENTENCE_END = re.compile(r"(?<=[.!?؟。])\s+")


@dataclass(frozen=True, slots=True)
class ChunkSpec:
    ordinal: int
    char_start: int
    char_end: int
    prefix: str
    text: str
    page: int | None
    heading_path: tuple[str, ...]
    element_type: str

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _prefix(heading_path: tuple[str, ...]) -> str:
    return f"[{' > '.join(heading_path)}]\n" if heading_path else ""


def _split_long(text: str, base: int, limit: int) -> list[tuple[int, int]]:
    """Split one element's text into (start, end) ranges relative to the canonical text."""

    ranges: list[tuple[int, int]] = []
    pieces: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        pieces.append((cursor, match.start()))
        cursor = match.end()
    pieces.append((cursor, len(text)))
    current: tuple[int, int] | None = None
    for piece_start, end in pieces:
        start = piece_start
        while end - start > limit:  # a single overlong sentence: split at whitespace
            cut = text.rfind(" ", start, start + limit)
            cut = cut if cut > start else start + limit
            if current:
                ranges.append(current)
                current = None
            ranges.append((start, cut))
            start = cut + 1 if cut < len(text) and text[cut] == " " else cut
        if start >= end:
            continue
        if current and end - current[0] <= limit:
            current = (current[0], end)
        else:
            if current:
                ranges.append(current)
            current = (start, end)
    if current:
        ranges.append(current)
    return [(base + s, base + e) for s, e in ranges if e > s]


def chunk_document(doc: CanonicalDocument, max_chars: int = MAX_CHARS) -> list[ChunkSpec]:
    text = doc.text
    chunks: list[ChunkSpec] = []
    group: list[PlacedElement] = []

    def emit(
        start: int, end: int, prefix: str, page: int | None, path: tuple[str, ...], kind: str
    ) -> None:
        body = text[start:end]
        chunks.append(ChunkSpec(len(chunks), start, end, prefix, prefix + body, page, path, kind))

    def flush() -> None:
        if not group:
            return
        first, last = group[0], group[-1]
        emit(
            first.start,
            last.end,
            _prefix(first.element.heading_path),
            first.element.page,
            first.element.heading_path,
            first.element.element_type,
        )
        group.clear()

    for placed in doc.elements:
        element = placed.element
        if element.element_type == "heading":
            flush()
            continue
        length = placed.end - placed.start
        if element.element_type == "table":
            flush()
            _chunk_table(placed, text, max_chars, emit)
            continue
        if element.element_type == "code":
            flush()
            for start, end in code_ranges(text, placed.start, placed.end, max_chars):
                emit(start, end, _prefix(element.heading_path), None, element.heading_path, "code")
            continue
        if length > max_chars:
            flush()
            for start, end in _split_long(element.text, placed.start, max_chars):
                emit(
                    start,
                    end,
                    _prefix(element.heading_path),
                    element.page,
                    element.heading_path,
                    element.element_type,
                )
            continue
        if group:
            same_section = (
                group[0].element.heading_path == element.heading_path
                and group[0].element.page == element.page
            )
            if not same_section or placed.end - group[0].start > max_chars:
                flush()
        group.append(placed)
    flush()
    return chunks


def chunker_version(doc: CanonicalDocument) -> str:
    """Recorded on the generation; code elements use the line chunker as well."""

    if any(p.element.element_type == "code" for p in doc.elements):
        return CODE_CHUNKER_VERSION
    return CHUNKER_VERSION


def code_ranges(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Split verbatim code into whole-line chunks of at most ``max_chars`` code points,
    preferring to cut after a blank line (between functions or blocks). A single line
    longer than the limit is cut at the limit. Ranges are contiguous and cover every
    non-blank line exactly once, so every chunk maps to exact source lines."""

    lines: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        newline = text.find("\n", cursor, end)
        stop = end if newline < 0 else newline
        lines.append((cursor, stop))
        cursor = stop + 1
    ranges: list[tuple[int, int]] = []
    group: list[tuple[int, int]] = []

    def close(upto: int) -> None:
        chunk = group[:upto]
        del group[:upto]
        first = next((s for s, e in chunk if e > s and text[s:e].strip()), None)
        if first is not None:
            last = max(e for s, e in chunk if text[s:e].strip())
            ranges.append((first, last))

    for first_char, line_end in lines:
        line_start = first_char
        while line_end - line_start > max_chars:  # an overlong line: hard cut
            if group:
                close(len(group))
            ranges.append((line_start, line_start + max_chars))
            line_start += max_chars
        if group and line_end - group[0][0] > max_chars:
            blank = [i for i, (s, e) in enumerate(group) if not text[s:e].strip()]
            cut = blank[-1] + 1 if blank and blank[-1] + 1 >= len(group) // 2 else len(group)
            close(cut)
        group.append((line_start, line_end))
    if group:
        close(len(group))
    return [(s, e) for s, e in ranges if e > s]


def _chunk_table(placed: PlacedElement, text: str, max_chars: int, emit) -> None:  # type: ignore[no-untyped-def]
    element = placed.element
    path_prefix = _prefix(element.heading_path)
    if placed.end - placed.start <= max_chars:
        emit(placed.start, placed.end, path_prefix, element.page, element.heading_path, "table")
        return
    body = text[placed.start : placed.end]
    header_end = element.header_end or body.find("\n")
    header = body[:header_end] if header_end > 0 else ""
    lines: list[tuple[int, int]] = []
    cursor = 0
    for line in body.split("\n"):
        lines.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    budget = max(max_chars - len(header) - 1, 80)
    group_start: int | None = None
    group_end = 0
    first_group = True
    for start, end in lines:
        if group_start is None:
            group_start = start
        elif end - group_start > budget:
            prefix = path_prefix if first_group else f"{path_prefix}{header}\n"
            emit(
                placed.start + group_start,
                placed.start + group_end,
                prefix,
                element.page,
                element.heading_path,
                "table",
            )
            first_group = False
            group_start = start
        group_end = end
    if group_start is not None:
        prefix = path_prefix if first_group else f"{path_prefix}{header}\n"
        emit(
            placed.start + group_start,
            placed.start + group_end,
            prefix,
            element.page,
            element.heading_path,
            "table",
        )
