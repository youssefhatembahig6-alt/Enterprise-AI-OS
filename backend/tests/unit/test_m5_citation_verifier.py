"""The independent code-citation check of scripts/m5_github_acceptance.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "m5_github_acceptance.py"
_spec = importlib.util.spec_from_file_location("m5_github_acceptance", SCRIPT)
assert _spec and _spec.loader
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

RAW = (
    b"\r\n\r\nA = 1\r\n\r\ndef f():\r\n    return 'caf\xc3\xa9'\r\n"  # CRLF, 2 leading blank lines
)
COMMIT = "c" * 40


def citation(**over: Any) -> dict[str, Any]:
    base = {
        "repository": "o/r",
        "path": "pkg/m.py",
        "commit": COMMIT,
        "blob_sha": runner.git_blob_sha(RAW),
        "line_start": 5,
        "line_end": 6,
        "quote": "def f():\n    return 'café'",
        "url": f"https://github.com/o/r/blob/{COMMIT}/pkg/m.py#L5-L6",
    }
    return {**base, **over}


def check(c: dict[str, Any]) -> list[str]:
    return list(runner.verify_code_citation(c, repo="o/r", path="pkg/m.py", commit=COMMIT, raw=RAW))


def test_an_exact_citation_has_no_problems() -> None:
    assert check(citation()) == []


def test_every_field_is_checked() -> None:
    assert any("repository" in p for p in check(citation(repository="o/other")))
    assert any("path" in p for p in check(citation(path="pkg/n.py")))
    assert any("commit" in p for p in check(citation(commit="d" * 40)))
    assert any("blob_sha" in p for p in check(citation(blob_sha="0" * 40)))
    assert any("outside" in p for p in check(citation(line_start=6, line_end=99)))
    # Off by one: the quote is in the file, but not at the cited lines.
    shifted = check(
        citation(
            line_start=4, line_end=5, url=f"https://github.com/o/r/blob/{COMMIT}/pkg/m.py#L4-L5"
        )
    )
    assert any("not inside lines" in p or "does not start" in p for p in shifted)
    assert any("not inside" in p for p in check(citation(quote="return 'tea'")))
    assert any("permalink" in p for p in check(citation(url="https://example.invalid")))


def test_quote_must_start_and_end_on_the_cited_lines() -> None:
    # "A = 1" is on line 3; citing lines 3-5 contains it but does not end on line 5.
    c = citation(
        line_start=3,
        line_end=5,
        quote="A = 1",
        url=f"https://github.com/o/r/blob/{COMMIT}/pkg/m.py#L3-L5",
    )
    assert any("end on line 5" in p for p in check(c))
