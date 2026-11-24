"""Verbatim code parsing, line-exact chunking and connector path rules (M5)."""

from __future__ import annotations

import pytest

from app.connectors import paths
from app.documents.canonical import line_range
from app.documents.chunker import CODE_CHUNKER_VERSION, chunk_document, chunker_version, code_ranges
from app.documents.parsers import ParseFailed, parse


def test_code_is_verbatim_and_lines_map_to_the_file() -> None:
    source = "\r\n\r\nimport os\r\n\r\n\r\ndef f():\r\n    return 'é'\r\n"
    doc = parse("pkg/mod.py", source.encode())
    assert doc.parser == "builtin-code-v1"
    assert doc.notes["first_line"] == 3 and doc.notes["verbatim_lines"] is True
    file_lines = source.replace("\r\n", "\n").split("\n")
    start = doc.text.index("return")
    first, last = line_range(doc.text, 3, start, start + len("return 'é'"))
    assert (first, last) == (7, 7)
    assert file_lines[first - 1].strip() == "return 'é'"


def test_repository_markdown_is_parsed_verbatim() -> None:
    data = b"# Title\n\n- item\n"
    assert parse("README.md", data).parser == "builtin-text-v1"
    assert parse("README.md", data, verbatim=True).parser == "builtin-code-v1"


def test_binary_code_files_are_refused() -> None:
    with pytest.raises(ParseFailed):
        parse("x.py", b"print(1)\x00\x00")


def test_code_chunks_are_whole_lines_and_cover_every_line() -> None:
    lines = [f"line_{i:03d} = {i}" for i in range(200)]
    text = "\n".join(lines[:100]) + "\n\n" + "\n".join(lines[100:])
    ranges = code_ranges(text, 0, len(text), 300)
    covered = "".join(text[s:e] + "\n" for s, e in ranges)
    for line in lines:
        assert line in covered
    for s, e in ranges:
        assert e - s <= 300
        assert s == 0 or text[s - 1] == "\n"  # starts at a line start
        assert e == len(text) or text[e] == "\n"  # ends at a line end


def test_overlong_lines_are_cut_at_the_limit() -> None:
    text = "x" * 1000
    assert code_ranges(text, 0, len(text), 400) == [(0, 400), (400, 800), (800, 1000)]


def test_code_documents_use_the_code_chunker() -> None:
    doc = parse("a.py", ("\n".join(f"v{i} = {i}" for i in range(300))).encode())
    chunks = chunk_document(doc)
    assert chunker_version(doc) == CODE_CHUNKER_VERSION
    assert all(c.element_type == "code" and c.prefix == "[a.py]\n" for c in chunks)


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("../x.md", "traversal"),
        ("a/../../x.md", "traversal"),
        ("/etc/passwd", "absolute"),
        ("C:/Windows/x.md", "absolute"),
        ("a\\b.md", "bad_characters"),
        ("a\x00b.md", "bad_characters"),
        ("a//b.md", "traversal"),
        ("/".join(["d"] * 21), "too_deep"),
        ("", "empty"),
    ],
)
def test_invalid_paths(path: str, error: str) -> None:
    with pytest.raises(paths.InvalidPath) as exc:
        paths.check_relative(path)
    assert str(exc.value) == error


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("notes/a.md", None),
        (".git/config", "hidden_folder"),
        ("src/.env", "hidden_file"),
        ("web/node_modules/x/index.js", "dependency_or_build_folder"),
        ("~$budget.xlsx", "temporary_or_generated_file"),
        ("report.md.tmp", "temporary_or_generated_file"),
        ("app.min.js", "temporary_or_generated_file"),
        ("package-lock.json", "excluded_file"),
        ("Thumbs.db", "excluded_file"),
    ],
)
def test_exclusions(path: str, reason: str | None) -> None:
    assert paths.exclusion(paths.check_relative(path)) == reason
