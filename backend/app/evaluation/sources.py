"""Resolve split-manifest member references to files and to canonical text.

Document and contract text is produced by the application's own native parser
(``app.documents.parsers.parse`` without Docling), so an evidence quote is checked
against exactly the text the application indexes. Code files are read verbatim.
"""

from __future__ import annotations

import hashlib
import tomllib
from functools import cache
from pathlib import Path

from app.documents.parsers import parse

BACKEND = Path(__file__).resolve().parents[2]
FIXTURES = BACKEND / "fixtures"
M7 = FIXTURES / "evaluation" / "m7"
KINDS = ("doc", "contract", "code", "sql")


class SourceError(ValueError):
    """A member reference that does not resolve to a known source."""


def split_ref(ref: str) -> tuple[str, str]:
    kind, sep, name = ref.partition(":")
    if not sep or kind not in KINDS or not name:
        raise SourceError(f"malformed source reference {ref!r}")
    return kind, name


@cache
def _documents() -> dict[str, Path]:
    corpus = tomllib.loads((FIXTURES / "documents" / "corpus.toml").read_text("utf-8"))
    return {d["key"]: FIXTURES / "documents" / "files" / d["file"] for d in corpus["documents"]}


@cache
def _contracts() -> dict[str, Path]:
    legal = tomllib.loads((FIXTURES / "legal" / "legal.toml").read_text("utf-8"))
    return {c["key"]: FIXTURES / "legal" / "files" / c["file"] for c in legal["contracts"]}


@cache
def _code_files() -> dict[str, Path]:
    root = FIXTURES / "github-acceptance"
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*.py"))}


def all_file_refs() -> list[str]:
    """Every source file the manifest must assign to exactly one family."""

    return (
        [f"doc:{k}" for k in sorted(_documents())]
        + [f"contract:{k}" for k in sorted(_contracts())]
        + [f"code:{k}" for k in sorted(_code_files())]
    )


def source_path(ref: str) -> Path | None:
    """The file behind a reference; ``None`` for an SQL template (no single file)."""

    kind, name = split_ref(ref)
    table = {"doc": _documents, "contract": _contracts, "code": _code_files}
    if kind == "sql":
        return None
    paths = table[kind]()
    if name not in paths:
        raise SourceError(f"unknown {kind} {name!r}")
    return paths[name]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@cache
def canonical_text(ref: str) -> str:
    path = source_path(ref)
    if path is None:
        raise SourceError(f"{ref} has no text; cite an SQL fixture value instead")
    if ref.startswith("code:"):
        return path.read_text("utf-8")
    return parse(path.name, path.read_bytes()).text
