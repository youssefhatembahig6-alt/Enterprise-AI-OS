"""Parsing, canonical text, chunking and quote location (no services)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.documents.answers import locate_quote, parse_answer, significant_terms
from app.documents.canonical import Element, build_canonical
from app.documents.chunker import MAX_CHARS, chunk_document
from app.documents.parsers import ParseFailed, detect, parse
from app.reporting.prompts import MalformedOutput

FILES = Path(__file__).resolve().parents[2] / "fixtures" / "documents" / "files"
M1_MANIFEST = (
    Path(__file__).resolve().parents[3] / "ml" / "parser-benchmark" / "fixtures" / "manifest.json"
)


def test_fixture_files_match_their_hashes() -> None:
    sums = (FILES.parent / "SHA256SUMS").read_text().split("\n")
    entries: dict[str, str] = {}
    for line in sums:
        if line.strip():
            digest, name = line.split("  ", 1)
            entries[name] = digest
    assert set(entries) == {p.name for p in FILES.iterdir()}
    for name, digest in entries.items():
        assert hashlib.sha256((FILES / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.skipif(not M1_MANIFEST.exists(), reason="ml/ not in this build")
def test_reused_m1_fixtures_are_byte_identical() -> None:
    manifest = json.loads(M1_MANIFEST.read_text())
    m1 = {Path(f["file"]).name: f["sha256"] for f in manifest["fixtures"]}
    reused = [n for n in m1 if (FILES / n).exists()]
    assert len(reused) == 5
    for name in reused:
        assert hashlib.sha256((FILES / name).read_bytes()).hexdigest() == m1[name]


@pytest.mark.parametrize(
    ("name", "parser"),
    [
        ("hr-leave-policy.md", "builtin-text-v1"),
        ("digital-company-overview.pdf", "pdfplumber-text-v1"),
        ("tricky-customers.csv", "python-csv-v1"),
        ("multi-sheet-q1.xlsx", "openpyxl-values-v1"),
    ],
)
def test_parsing_is_deterministic_and_chunks_are_exact_spans(name: str, parser: str) -> None:
    data = (FILES / name).read_bytes()
    first, second = parse(name, data), parse(name, data)
    assert first.parser == parser
    assert first.text_sha256 == second.text_sha256
    chunks = chunk_document(first)
    assert chunks == chunk_document(second)
    for chunk in chunks:
        body = first.text[chunk.char_start : chunk.char_end]
        assert chunk.text == chunk.prefix + body
        assert len(body) <= MAX_CHARS or chunk.element_type == "table"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_scan_needs_ocr_and_hidden_sheet_is_not_indexed() -> None:
    with pytest.raises(ParseFailed) as excinfo:
        parse("scanned-delivery-note.pdf", (FILES / "scanned-delivery-note.pdf").read_bytes())
    assert excinfo.value.code == "needs_ocr"
    workbook = parse("multi-sheet-q1.xlsx", (FILES / "multi-sheet-q1.xlsx").read_bytes())
    assert workbook.notes["hidden_sheets_not_indexed"] == ["Lookup"]


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("notes.docx", b"PK\x03\x04", "unsupported_type"),
        ("broken.pdf", b"not a pdf", "malformed_file"),
        ("broken.pdf", b"%PDF-1.7\ngarbage without objects", "malformed_file"),
        ("broken.xlsx", b"PK\x03\x04garbage", "malformed_file"),
        ("empty.md", b"", "empty_document"),
        ("blank.md", b"\n\n   \n", "empty_document"),
    ],
)
def test_malformed_files_fail_with_useful_codes(name: str, data: bytes, code: str) -> None:
    with pytest.raises(ParseFailed) as excinfo:
        parse(name, data)
    assert excinfo.value.code == code
    assert excinfo.value.message


def test_detect_rejects_mismatched_signatures() -> None:
    with pytest.raises(ParseFailed):
        detect("report.pdf", b"PK\x03\x04")
    assert detect("Notes.MD", b"# hi").media_type == "text/markdown"


def test_canonical_offsets_count_code_points_and_normalize() -> None:
    doc = build_canonical(
        [Element("paragraph", "Café \U0001f468‍\U0001f469‍\U0001f467 résumé\r\nline")],
        "test",
    )
    assert doc.text == "Café \U0001f468‍\U0001f469‍\U0001f467 résumé\nline"
    assert len(doc.text) == 22  # code points: the ZWJ family is 5 of them
    assert len(doc.text.encode("utf-16-le")) // 2 == 25  # what JavaScript would count


def test_repeated_passage_is_located_inside_the_cited_chunk() -> None:
    doc = parse("hr-leave-policy.md", (FILES / "hr-leave-policy.md").read_bytes())
    phrase = "Requests must be approved by the line manager."
    occurrences = [i for i in range(len(doc.text)) if doc.text.startswith(phrase, i)]
    assert len(occurrences) == 2
    chunks = [c for c in chunk_document(doc) if phrase in c.text]
    assert len(chunks) == 2
    spans = []
    for chunk in chunks:
        body = doc.text[chunk.char_start : chunk.char_end]
        start, end = locate_quote(body, phrase)  # type: ignore[misc]
        spans.append((chunk.char_start + start, chunk.char_start + end))
    assert sorted(s for s, _ in spans) == occurrences
    for start, end in spans:
        assert doc.text[start:end] == phrase


def test_table_rows_and_non_ascii_quotes_round_trip() -> None:
    doc = parse("hr-leave-policy.md", (FILES / "hr-leave-policy.md").read_bytes())
    for quote in (
        "UAE (الإمارات) | 30 | 30",
        "Eid al-Fitr (عيد الفطر)",
        "Family leave \U0001f468‍\U0001f469‍\U0001f467",
    ):
        span = locate_quote(doc.text, quote)
        assert span is not None, quote
        assert doc.text[span[0] : span[1]] == quote


def test_long_tables_repeat_their_header() -> None:
    rows = ["Name | Value"] + [f"row-{i:03d} | {i * 1000}" for i in range(120)]
    doc = build_canonical([Element("table", "\n".join(rows), header_end=len(rows[0]))], "test")
    chunks = chunk_document(doc)
    assert len(chunks) > 1
    assert all(c.prefix.endswith("Name | Value\n") for c in chunks[1:])
    covered = "".join(doc.text[c.char_start : c.char_end] + "\n" for c in chunks)
    assert covered.strip() == doc.text


def test_whitespace_tolerant_quote_maps_to_exact_text() -> None:
    body = "Up to five unused days may be carried\ninto the first quarter."
    span = locate_quote(body, "carried into the first quarter")
    assert span is not None and body[span[0] : span[1]] == "carried\ninto the first quarter"
    assert locate_quote(body, "carried over to the next year") is None


def test_answer_parsing_and_terms() -> None:
    assert parse_answer('{"answerable": false}') == (False, [])
    ok, sentences = parse_answer(
        '{"answerable": true, "sentences": [{"text": "x", "cites": ["s1"], "quote": "q"}]}'
    )
    assert ok and sentences[0]["cites"] == ["s1"]
    for bad in (
        '{"answerable": true}',
        '{"answerable": true, "sentences": [{"text": "x"}]}',
        "nope",
    ):
        with pytest.raises(MalformedOutput):
            parse_answer(bad)
    assert significant_terms("What is the leave in the UAE?") == {"leave", "uae"}
    assert significant_terms("minimum password length") & significant_terms(
        "Passwords must be long"
    )
    assert significant_terms("overdue invoices") & significant_terms("one overdue invoice")
