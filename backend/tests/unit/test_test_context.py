"""The backend test image's build context (scripts/test_context.py) keeps private and
local material out: untracked, ignored and label files, other branches, private refs and
the shared object database. Checked with synthetic sentinels from a normal clone and from
a linked worktree."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from app.evaluation import loader

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "test_context.py"
# The real label directory, through the loader (the only sanctioned path to M7 files).
LABELS = (loader.M7_DIR / "pilot" / "labels").relative_to(REPO).as_posix()
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
SENTINELS = {
    "untracked label": "SENTINEL-UNTRACKED-LABEL-4c1f",
    "tracked label": "SENTINEL-TRACKED-LABEL-9a2e",
    "ignored var": "SENTINEL-IGNORED-VAR-7d30",
    "ignored env": "SENTINEL-IGNORED-ENV-1b8c",
    "ignored venv": "SENTINEL-IGNORED-VENV-e5a1",
    "untracked doc": "SENTINEL-UNTRACKED-DOC-3f62",
    "outside trees": "SENTINEL-OUTSIDE-TREES-80d4",
    "other branch": "SENTINEL-OTHER-BRANCH-62bb",
    "private ref": "SENTINEL-PRIVATE-REF-c07a",
    "history": "SENTINEL-OLD-HISTORY-a91d",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_context_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return done.stdout.decode().strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str]:
    """A repository shaped like the checkout, with history, another branch and a private
    ref, all carrying sentinels that must not reach the test context."""

    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo / ".gitignore", "var/\n.env\n.env.*\n.venv/\n")
    write(repo / "backend/app/x.py", "x = 1\n")
    write(repo / "backend/old.txt", SENTINELS["history"])
    write(repo / f"{LABELS}/README.md", "labels live elsewhere\n")
    write(repo / "scripts/s.py", "print('s')\n")
    write(repo / "ml/m.md", "ml\n")
    write(repo / "docs/d.md", "docs\n")
    write(repo / "apps/outside.txt", SENTINELS["outside trees"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    (repo / "backend/old.txt").unlink()
    write(repo / f"{LABELS}/gold-annotator-a.csv", SENTINELS["tracked label"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "drop old file; tracked label")
    git(repo, "branch", "-q", "other")
    git(repo, "checkout", "-q", "other")
    write(repo / "backend/other.txt", SENTINELS["other branch"])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "other branch")
    git(repo, "checkout", "-q", "main")
    blob = git(repo, "hash-object", "-w", "--stdin", stdin=SENTINELS["private ref"].encode())
    tree = git(repo, "mktree", stdin=f"100644 blob {blob}\tgold-private.csv\n".encode())
    commit = git(repo, "commit-tree", tree, "-m", "private labels")
    git(repo, "update-ref", "refs/private/m7-labels", commit)
    return repo, commit


def add_local_sentinels(checkout: Path) -> None:
    write(checkout / f"{LABELS}/gold-annotator-b.csv", SENTINELS["untracked label"])
    write(checkout / "var/m7-labels/gold.csv", SENTINELS["ignored var"])
    write(checkout / "backend/.env", SENTINELS["ignored env"])
    write(checkout / "ml/bench/.venv/lib.py", SENTINELS["ignored venv"])
    write(checkout / "docs/untracked-notes.md", SENTINELS["untracked doc"])


def all_bytes(root: Path) -> bytes:
    """Every file's bytes plus every git object's content in ``root``'s repository."""

    out = b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
    objects = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    return out + objects


def assert_isolated(context: Path, manifest: dict[str, object], private_commit: str) -> None:
    files = sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file()
                   and ".git" not in p.relative_to(context).parts)  # fmt: skip
    assert files == [
        ".eaios-test-context",
        "backend/app/x.py",
        f"{LABELS}/README.md",
        "docs/d.md",
        "ml/m.md",
        "scripts/s.py",
    ]
    assert manifest["dropped"] == [f"{LABELS}/gold-annotator-a.csv"]
    blob = all_bytes(context)
    leaked = [name for name, value in SENTINELS.items() if value.encode() in blob]
    assert leaked == []
    # Only the snapshot: one commit, one ref, no alternates, no private object.
    assert git(context, "rev-list", "--all", "--count") == "1"
    assert git(context, "for-each-ref", "--format=%(refname)") == "refs/heads/test-context"
    assert not (context / ".git/objects/info/alternates").exists()
    probe = subprocess.run(  # noqa: S603
        ["git", "cat-file", "-e", private_commit],  # noqa: S607
        cwd=context,
        capture_output=True,
        check=False,
    )
    assert probe.returncode != 0
    assert json.loads((context / ".eaios-test-context").read_text())["files"] == 5


def test_normal_clone_stages_only_indexed_files_and_a_snapshot_repo(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    repo, private_commit = origin
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-local", str(repo), str(clone))
    git(clone, "fetch", "-q", "origin", "refs/private/m7-labels:refs/private/m7-labels")
    add_local_sentinels(clone)
    context = tmp_path / "ctx"
    manifest = load().stage(clone, context)
    assert manifest["source_head"] == git(clone, "rev-parse", "HEAD")
    assert manifest["source_dirty"] is False
    assert_isolated(context, manifest, private_commit)


def test_linked_worktree_stages_the_same_way(origin: tuple[Path, str], tmp_path: Path) -> None:
    repo, private_commit = origin
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(worktree), "main", "--force")
    assert (worktree / ".git").is_file()  # a gitfile pointing at the shared database
    add_local_sentinels(worktree)
    (worktree / "backend/app/x.py").write_text("x = 2  # uncommitted edit\n")
    context = tmp_path / "ctx"
    manifest = load().stage(worktree / "backend", context)  # from a subdirectory too
    assert manifest["source_dirty"] is True
    assert (context / "backend/app/x.py").read_text() == "x = 2  # uncommitted edit\n"
    assert_isolated(context, manifest, private_commit)


def test_the_real_label_directory_is_excluded() -> None:
    from app.evaluation import loader as m7_loader

    labels = (m7_loader.M7_DIR / "pilot" / "labels").relative_to(REPO).as_posix()
    module = load()
    assert module.is_excluded(f"{labels}/gold-annotator-a.csv")
    assert module.is_excluded(f"{labels}/.import-staging-1234/gold.csv")
    assert not module.is_excluded(f"{labels}/README.md")
    assert not module.is_excluded("backend/fixtures/documents/files/memo.md")
    assert not module.is_excluded("docs/pilot/labels/notes.md")  # outside fixtures


def test_refuses_a_non_empty_destination(origin: tuple[Path, str], tmp_path: Path) -> None:
    repo, _ = origin
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "file").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        load().stage(repo, tmp_path / "busy")


def test_refuses_a_symlink_that_leaves_the_checkout(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _ = origin
    (repo / "docs/escape").symlink_to("/etc/hostname")
    git(repo, "add", "docs/escape")
    with pytest.raises(SystemExit, match="symlink leaving the checkout"):
        load().stage(repo, tmp_path / "ctx")
