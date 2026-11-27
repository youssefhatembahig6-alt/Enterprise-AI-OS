#!/usr/bin/env python3
"""Stage the build context for the backend test image (``backend-tests``).

The test image needs the checkout's ``backend/``, ``scripts/``, ``ml/`` and ``docs/``
trees and a git binary, but never the developer's private or local material. The
context is a fresh directory holding only:

* **Files in the git index** under those four trees, with their working-tree contents
  (uncommitted edits to tracked files are tested; new files must be ``git add``-ed).
* minus **anything git would ignore, even if it was force-added** (``var/``, ``.env``,
  keys, virtualenvs, raw GPU output ...),
* minus **private material by name, whatever the ignore rules say**: ``.env`` files other
  than ``.env.example``, ``*.pem``/``*.key``, anything under a ``var/``, ``.venv/``,
  ``venv/`` or ``node_modules/`` directory, and human-label submissions (files in the
  fixtures' ``pilot/labels/`` directory other than its README),
* and only paths whose **resolved location** is the expected file inside the checkout:
  a tracked path reached through a symlinked parent directory, or a symlink pointing
  outside the checkout or at an unstaged path, stops staging before anything is written.

There is **no git repository in the context**: the tests need only the git binary (they
build their own temporary repositories), so nothing from the checkout's ``.git`` is
copied, mounted or recreated. A manifest, ``.eaios-test-context``, records what was
staged; ``backend/Dockerfile.test`` copies it first, so building from an unstaged
directory fails.

Git is only *read*, with an environment scrubbed of every ``GIT_*`` variable (an
inherited ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` would otherwise point the reads
at another repository), with optional locks off (so ``git status`` does not rewrite the
index) and ``core.fsmonitor`` off. Staging leaves the source checkout's HEAD, refs and
index untouched. It works the same from a normal clone and a linked worktree.

    python3 scripts/test_context.py <empty or missing directory>   # stage only
    (used by ``scripts/dev.py test-backend`` and ``scripts/m7_acceptance.py test-backend``)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

TREES = ("backend", "scripts", "ml", "docs")
# Human-label submissions live in a ``pilot/labels/`` directory under backend/fixtures
# (the M7 pilot's label directory; see app.evaluation.labels.LABEL_DIR). Only READMEs
# there are staged.
FIXTURES = "backend/fixtures/"
LABEL_SEGMENT = "/pilot/labels/"
PRIVATE_DIRS = frozenset({"var", ".venv", "venv", "node_modules"})
PRIVATE_SUFFIXES = (".pem", ".key")
MARKER = ".eaios-test-context"


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # read-only: no index refresh by `git status`
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path, *args: str) -> bytes:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "core.fsmonitor=false", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        check=False,
        env=_git_env(),
    )
    if done.returncode != 0:
        raise SystemExit(f"git {args[0]} failed in {cwd}: {done.stderr.decode().strip()}")
    return done.stdout


def _paths(raw: bytes) -> list[str]:
    return [p.decode() for p in raw.split(b"\0") if p]


def is_label(path: str) -> bool:
    """True for files in a fixtures ``pilot/labels/`` directory other than README.md."""

    return (
        path.startswith(FIXTURES)
        and LABEL_SEGMENT in path
        and PurePosixPath(path).name != "README.md"
    )


def is_private_name(path: str) -> bool:
    """Private by name, independent of any ignore file."""

    parts = PurePosixPath(path).parts
    name = parts[-1]
    if any(part in PRIVATE_DIRS for part in parts[:-1]):
        return True
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return True
    return name.endswith(PRIVATE_SUFFIXES)


def is_excluded(path: str) -> bool:
    return is_label(path) or is_private_name(path)


def _check_location(top: Path, rel: str) -> None:
    """Refuse a tracked path that does not resolve to itself inside the checkout."""

    src = top / rel
    expected_parent = top / PurePosixPath(rel).parent
    if src.parent.resolve(strict=True) != expected_parent:
        raise SystemExit(f"{rel}: a parent directory is a symlink; refusing to stage")
    if src.is_symlink():
        target = src.resolve(strict=False)
        if not target.is_relative_to(top):
            raise SystemExit(f"{rel}: symlink leaving the checkout; refusing to stage")
        inside = target.relative_to(top).as_posix()
        if not inside.startswith(tuple(t + "/" for t in TREES)) or is_excluded(inside):
            raise SystemExit(f"{rel}: symlink to a path that is not staged; refusing to stage")
    elif src.resolve(strict=True) != src:
        raise SystemExit(f"{rel}: does not resolve to itself; refusing to stage")


def stage(source: Path, dest: Path) -> dict[str, object]:
    """Copy the allowed files of the checkout containing ``source`` into ``dest`` (which
    must be empty or missing). Returns the manifest."""

    top = Path(_git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    head = _git(top, "rev-parse", "HEAD").decode().strip()
    dirty = bool(_git(top, "status", "--porcelain", "--untracked-files=no", "--", *TREES))
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")

    indexed = _paths(_git(top, "ls-files", "-z", "--cached", "--", *TREES))
    ignored = set(
        _paths(
            _git(top, "ls-files", "-z", "--cached", "--ignored", "--exclude-standard", "--", *TREES)
        )
    )
    dropped: dict[str, list[str]] = {"ignored": [], "private": [], "labels": []}
    plan: list[str] = []
    for rel in indexed:
        if rel in ignored:
            dropped["ignored"].append(rel)
        elif is_label(rel):
            dropped["labels"].append(rel)
        elif is_private_name(rel):
            dropped["private"].append(rel)
        elif (top / rel).is_symlink() or (top / rel).is_file():
            plan.append(rel)  # else: deleted in the working tree, or a submodule
    for rel in plan:  # every location is checked before anything is written
        _check_location(top, rel)

    dest.mkdir(parents=True, exist_ok=True)
    for rel in plan:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(top / rel, out, follow_symlinks=False)

    manifest: dict[str, object] = {
        "source_head": head,
        "source_dirty": dirty,
        "trees": list(TREES),
        "files": len(plan),
        "dropped": dropped,
    }
    (dest / MARKER).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


@contextmanager
def staged(source: Path) -> Iterator[Path]:
    """A staged context in a fresh temporary directory, removed afterwards."""

    parent = Path(tempfile.mkdtemp(prefix="eaios-test-context-"))
    try:
        dest = parent / "context"
        manifest = stage(source, dest)
        dropped = manifest["dropped"]
        assert isinstance(dropped, dict)
        counts = ", ".join(f"{k} {len(v)}" for k, v in dropped.items())
        print(
            f"test context: {manifest['files']} files from {str(manifest['source_head'])[:12]}"
            f"{' + uncommitted changes' if manifest['source_dirty'] else ''};"
            f" dropped indexed files: {counts}; {dest}",
            flush=True,
        )
        yield dest
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    manifest = stage(Path.cwd(), Path(sys.argv[1]))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
