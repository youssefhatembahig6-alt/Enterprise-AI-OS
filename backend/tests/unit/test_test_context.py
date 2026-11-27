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
    write(repo / ".gitignore", "var/\n.env\n.env.*\n!.env.example\n.venv/\n")
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
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


EXPECTED_FILES = [
    ".eaios-test-context",
    "backend/app/x.py",
    f"{LABELS}/README.md",
    "docs/d.md",
    "ml/m.md",
    "scripts/s.py",
]


def staged_files(context: Path) -> list[str]:
    return sorted(p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file())


def assert_isolated(context: Path, manifest: dict[str, object]) -> None:
    assert staged_files(context) == EXPECTED_FILES
    assert manifest["dropped"] == {
        "ignored": [],
        "private": [],
        "labels": [f"{LABELS}/gold-annotator-a.csv"],
    }
    leaked = [name for name, value in SENTINELS.items() if value.encode() in all_bytes(context)]
    assert leaked == []
    # No git repository at all: nothing from the source's object database, refs or config.
    assert not any(p.name == ".git" for p in context.rglob("*"))
    assert json.loads((context / ".eaios-test-context").read_text())["files"] == 5


def snapshot_of(repo: Path) -> tuple[str, str, bytes, int]:
    """HEAD, every ref, and the index bytes and mtime of ``repo``'s git directory."""

    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    index = git_dir / "index"
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "for-each-ref", "--format=%(objectname) %(refname)"),
        index.read_bytes(),
        index.stat().st_mtime_ns,
    )


def test_normal_clone_stages_only_allowed_files_and_no_repository(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _ = origin
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--no-local", str(repo), str(clone))
    git(clone, "fetch", "-q", "origin", "refs/private/m7-labels:refs/private/m7-labels")
    add_local_sentinels(clone)
    context = tmp_path / "ctx"
    manifest = load().stage(clone, context)
    assert manifest["source_head"] == git(clone, "rev-parse", "HEAD")
    assert manifest["source_dirty"] is False
    assert_isolated(context, manifest)


def test_linked_worktree_stages_the_same_way(origin: tuple[Path, str], tmp_path: Path) -> None:
    repo, _ = origin
    worktree = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(worktree), "main", "--force")
    assert (worktree / ".git").is_file()  # a gitfile pointing at the shared database
    add_local_sentinels(worktree)
    (worktree / "backend/app/x.py").write_text("x = 2  # uncommitted edit\n")
    context = tmp_path / "ctx"
    manifest = load().stage(worktree / "backend", context)  # from a subdirectory too
    assert manifest["source_dirty"] is True
    assert (context / "backend/app/x.py").read_text() == "x = 2  # uncommitted edit\n"
    assert_isolated(context, manifest)


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


# -- review fixes: git environment, force-tracked private files, symlinked parents -------


@pytest.mark.parametrize("target", ["source", "other"])
def test_inherited_git_routing_variables_neither_redirect_nor_modify_the_source(
    origin: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    """An inherited GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE once made staging commit into
    the source checkout. Staging now writes no repository and reads git with a scrubbed
    environment: the source's HEAD, refs and index are byte-for-byte unchanged, and the
    files come from the checkout being staged, not from where the variables point."""

    repo, _ = origin
    (repo / "backend/app/x.py").write_text("x = 3  # staged-from-source marker\n")
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    write(other / "backend/app/x.py", "OTHER-REPOSITORY\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "other")
    before_source, before_other = snapshot_of(repo), snapshot_of(other)
    routed = repo if target == "source" else other
    monkeypatch.setenv("GIT_DIR", str(routed / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(routed))
    monkeypatch.setenv("GIT_INDEX_FILE", str(routed / ".git" / "index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(routed / ".git" / "objects"))
    context = tmp_path / "ctx"
    manifest = load().stage(repo / "docs", context)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name)
    assert snapshot_of(repo) == before_source
    assert snapshot_of(other) == before_other
    assert manifest["source_head"] == before_source[0]
    assert (context / "backend/app/x.py").read_text() == "x = 3  # staged-from-source marker\n"
    assert not (context / ".git").exists()


def test_force_tracked_ignored_and_private_files_are_never_staged(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    """Indexed is not enough: a force-added ignored file (backend/.env) and private
    names that no ignore rule covers stay out."""

    repo, _ = origin
    private = {
        "backend/.env": "SENTINEL-FORCED-ENV-11aa",  # ignored by .gitignore, force-added
        "ml/bench/.venv/site.py": "SENTINEL-FORCED-VENV-22bb",  # ignored, force-added
        "backend/.env.local": "SENTINEL-ENV-LOCAL-33cc",  # ignored (.env.*), force-added
        "docs/var/cache.txt": "SENTINEL-NESTED-VAR-44dd",  # ignored ("var/" matches anywhere)
        "backend/venv/lib.py": "SENTINEL-VENV-DIR-44ee",  # no ignore rule: by name
        "scripts/deploy.pem": "SENTINEL-PEM-55ee",  # no ignore rule: by name
        "backend/keys/signing.key": "SENTINEL-KEY-66ff",  # no ignore rule: by name
        "ml/node_modules/x.js": "SENTINEL-NODE-MODULES-7700",  # no ignore rule: by name
    }
    for rel, text in private.items():
        write(repo / rel, text)
        git(repo, "add", "-f", rel)
    write(repo / "backend/.env.example", "EXAMPLE=1\n")
    git(repo, "add", "backend/.env.example")
    context = tmp_path / "ctx"
    manifest = load().stage(repo, context)
    dropped = manifest["dropped"]
    assert isinstance(dropped, dict)
    assert sorted(dropped["ignored"]) == sorted(
        ["backend/.env", "backend/.env.local", "docs/var/cache.txt", "ml/bench/.venv/site.py"]
    )
    assert sorted(dropped["private"]) == sorted(
        ["backend/keys/signing.key", "backend/venv/lib.py", "ml/node_modules/x.js",
         "scripts/deploy.pem"]
    )  # fmt: skip
    assert staged_files(context) == sorted([*EXPECTED_FILES, "backend/.env.example"])
    blob = all_bytes(context)
    assert [rel for rel, text in private.items() if text.encode() in blob] == []


def test_a_symlinked_parent_directory_is_refused_before_anything_is_copied(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    """A tracked file whose parent directory was replaced by a symlink to a directory
    outside the checkout must not be read through it."""

    repo, _ = origin
    write(repo / "docs/guide/page.md", "inside\n")
    git(repo, "add", "docs/guide/page.md")
    git(repo, "commit", "-q", "-m", "guide")
    outside = tmp_path / "outside"
    write(outside / "page.md", "SENTINEL-OUTSIDE-VIA-PARENT-88aa")
    for child in (repo / "docs/guide").iterdir():
        child.unlink()
    (repo / "docs/guide").rmdir()
    (repo / "docs/guide").symlink_to(outside, target_is_directory=True)
    context = tmp_path / "ctx"
    with pytest.raises(SystemExit, match=r"docs/guide/page\.md: a parent directory is a symlink"):
        load().stage(repo, context)
    assert not context.exists()  # checked before anything was written


def test_a_symlinked_parent_inside_the_checkout_is_refused_too(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _ = origin
    write(repo / "docs/guide/page.md", "inside\n")
    git(repo, "add", "docs/guide/page.md")
    git(repo, "commit", "-q", "-m", "guide")
    write(repo / "var/private/page.md", "SENTINEL-VAR-VIA-PARENT-99bb")  # ignored
    for child in (repo / "docs/guide").iterdir():
        child.unlink()
    (repo / "docs/guide").rmdir()
    (repo / "docs/guide").symlink_to(repo / "var/private", target_is_directory=True)
    with pytest.raises(SystemExit, match="a parent directory is a symlink"):
        load().stage(repo, tmp_path / "ctx")


def test_a_symlink_to_an_unstaged_path_is_refused(origin: tuple[Path, str], tmp_path: Path) -> None:
    repo, _ = origin
    write(repo / "var/secret.txt", "SENTINEL-SYMLINK-TARGET-aa11")
    (repo / "docs/shortcut").symlink_to("../var/secret.txt")
    git(repo, "add", "docs/shortcut")
    with pytest.raises(SystemExit, match="symlink to a path that is not staged"):
        load().stage(repo, tmp_path / "ctx")


def test_a_symlink_between_staged_files_is_kept_as_a_link(
    origin: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _ = origin
    (repo / "docs/alias.md").symlink_to("d.md")
    git(repo, "add", "docs/alias.md")
    context = tmp_path / "ctx"
    load().stage(repo, context)
    assert (context / "docs/alias.md").is_symlink()
    assert (context / "docs/alias.md").read_text() == "docs\n"
