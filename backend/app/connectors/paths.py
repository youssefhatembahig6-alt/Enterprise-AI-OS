"""Path rules shared by the repository and local-folder connectors (M5).

``check_relative`` rejects anything that is not a plain relative path inside the
source (absolute paths, ``..``, backslashes, control characters, excessive depth).
``exclusion`` names why a valid path is never ingested (dot files and folders,
dependency and build folders, editor/OS temporary files, lock files). The desktop
app applies the same rules before uploading (``apps/desktop/src/sync/rules.ts``); the
backend re-applies them, so a modified client cannot widen what is ingested.
"""

from __future__ import annotations

import re
import unicodedata

MAX_PATH_CHARS = 1024
MAX_DEPTH = 20
MAX_SEGMENT_CHARS = 255
EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "coverage",
        "__pycache__",
        "venv",
        "site-packages",
    }
)
EXCLUDED_FILES = frozenset(
    {
        "thumbs.db",
        "desktop.ini",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "cargo.lock",
        "composer.lock",
    }
)
EXCLUDED_SUFFIXES = (".tmp", ".temp", ".swp", ".swx", ".part", ".crdownload", ".bak", ".map")
EXCLUDED_PATTERNS = (re.compile(r"^~\$"), re.compile(r"~$"), re.compile(r"\.min\.(js|css)$"))
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InvalidPath(ValueError):
    pass


def check_relative(path: str) -> str:
    """Return the NFC-normalised relative path or raise ``InvalidPath``."""

    if not isinstance(path, str) or not path:
        raise InvalidPath("empty")
    value = unicodedata.normalize("NFC", path)
    if len(value) > MAX_PATH_CHARS:
        raise InvalidPath("too_long")
    if _CONTROL.search(value) or "\\" in value:
        raise InvalidPath("bad_characters")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise InvalidPath("absolute")
    parts = value.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise InvalidPath("traversal")
    if len(parts) > MAX_DEPTH:
        raise InvalidPath("too_deep")
    if any(len(p) > MAX_SEGMENT_CHARS for p in parts):
        raise InvalidPath("segment_too_long")
    return value


def exclusion(path: str) -> str | None:
    """Why this (valid) relative path is excluded, or ``None``."""

    parts = path.split("/")
    for part in parts[:-1]:
        if part.startswith("."):
            return "hidden_folder"
        if part.lower() in EXCLUDED_DIRS:
            return "dependency_or_build_folder"
    name = parts[-1]
    lowered = name.lower()
    if name.startswith("."):
        return "hidden_file"
    if lowered in EXCLUDED_FILES:
        return "excluded_file"
    if lowered.endswith(EXCLUDED_SUFFIXES) or any(p.search(name) for p in EXCLUDED_PATTERNS):
        return "temporary_or_generated_file"
    return None


def extension(path: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]+)$", path.rsplit("/", 1)[-1])
    return match.group(1).lower() if match else ""
