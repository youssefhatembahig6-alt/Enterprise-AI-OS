#!/usr/bin/env python3
"""Stage the build context for the backend test image (``backend-tests``).

The test image needs the checkout's ``backend/``, ``scripts/``, ``ml/`` and ``docs/``
trees and a git binary, but never the developer's private or local material. The
context is therefore a fresh directory that holds:

* **Only files in the git index** under those four trees, with their working-tree
  contents, so uncommitted edits to tracked files are tested. Untracked files, ignored
  files (``var/``, ``.env``, keys, virtualenvs, raw GPU output) and anything outside the
  four trees never enter.
* **No human-label submissions**, even when tracked (for example while a private labels
  branch is checked out). Everything in the M7 pilot's ``pilot/labels/`` directory under
  ``backend/fixtures`` except its README is dropped. The test suite runs without human labels, as in CI.
* **A one-commit git repository built from exactly those files.** No object, ref or
  config is copied from the checkout's ``.git``; the shared object database, other
  branches and private refs stay out. The tests need no real history (they build their
  own temporary repositories); the snapshot gives ``git rev-parse HEAD`` a value and
  records the source commit in its message.
* ``.eaios-test-context``: a manifest. ``backend/Dockerfile.test`` copies it first, so
  building from anything but a staged context fails.

Works the same from a normal clone and from a linked worktree (``git worktree add``).

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
from pathlib import Path

TREES = ("backend", "scripts", "ml", "docs")
# Human-label submissions live in a ``pilot/labels/`` directory under backend/fixtures
# (the M7 pilot's label directory; see app.evaluation.labels.LABEL_DIR). Only READMEs
# there are staged.
FIXTURES = "backend/fixtures/"
LABEL_SEGMENT = "/pilot/labels/"
MARKER = ".eaios-test-context"
# Fixed identity and dates: the snapshot commit depends only on the staged files.
_SNAPSHOT_ENV = {
    "GIT_AUTHOR_NAME": "eaios test context",
    "GIT_AUTHOR_EMAIL": "test-context@eaios.invalid",
    "GIT_COMMITTER_NAME": "eaios test context",
    "GIT_COMMITTER_EMAIL": "test-context@eaios.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    done = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )
    if done.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {done.stderr.decode().strip()}")
    return done.stdout


def is_excluded(path: str) -> bool:
    """True for files in a fixtures ``pilot/labels/`` directory other than README.md."""

    return (
        path.startswith(FIXTURES)
        and LABEL_SEGMENT in path
        and Path(path).name != "README.md"
    )


def stage(source: Path, dest: Path) -> dict[str, object]:
    """Copy the allowed files of the checkout containing ``source`` into ``dest`` (which
    must be empty or missing) and give it a snapshot repository. Returns the manifest."""

    top = Path(_git(source, "rev-parse", "--show-toplevel").decode().strip())
    head = _git(top, "rev-parse", "HEAD").decode().strip()
    dirty = bool(_git(top, "status", "--porcelain", "--untracked-files=no", "--", *TREES))
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)

    listed = _git(top, "ls-files", "-z", "--cached", "--", *TREES).split(b"\0")
    copied: list[str] = []
    dropped: list[str] = []
    for raw in listed:
        if not raw:
            continue
        rel = raw.decode()
        src = top / rel
        if is_excluded(rel):
            dropped.append(rel)
            continue
        if not src.is_symlink() and not src.is_file():
            continue  # deleted in the working tree, or a submodule
        if src.is_symlink():
            target = (src.parent / os.readlink(src)).resolve()
            if not target.is_relative_to(top):
                raise SystemExit(f"{rel} is a symlink leaving the checkout; refusing to stage")
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out, follow_symlinks=False)
        copied.append(rel)

    manifest: dict[str, object] = {
        "source_head": head,
        "source_dirty": dirty,
        "trees": list(TREES),
        "files": len(copied),
        "dropped": dropped,
    }
    (dest / MARKER).write_text(json.dumps(manifest, indent=2) + "\n")

    _git(dest, "init", "-q", "-b", "test-context", env=_SNAPSHOT_ENV)
    _git(dest, "add", "-A", "-f", env=_SNAPSHOT_ENV)
    message = f"test context snapshot of {head}{' (with uncommitted changes)' if dirty else ''}"
    _git(dest, "commit", "-q", "--no-gpg-sign", "-m", message, env=_SNAPSHOT_ENV)
    manifest["snapshot_head"] = _git(dest, "rev-parse", "HEAD").decode().strip()
    return manifest


@contextmanager
def staged(source: Path) -> Iterator[Path]:
    """A staged context in a fresh temporary directory, removed afterwards."""

    parent = Path(tempfile.mkdtemp(prefix="eaios-test-context-"))
    try:
        dest = parent / "context"
        manifest = stage(source, dest)
        print(
            f"test context: {manifest['files']} files from {manifest['source_head'][:12]}"
            f"{' + uncommitted changes' if manifest['source_dirty'] else ''};"
            f" dropped {len(manifest['dropped'])} label file(s); {dest}",  # type: ignore[arg-type]
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
