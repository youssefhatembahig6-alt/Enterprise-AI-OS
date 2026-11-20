"""Explicit, idempotent development seeding."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.seed import SeedRefused, seed_foundation, write_credentials
from tests.integration.conftest import DbHarness, owner_exec

COUNTED = (
    "tenants",
    "users",
    "roles",
    "groups",
    "group_memberships",
    "resources",
    "resource_grants",
    "user_roles",
    "role_capabilities",
)


def counts(test_db: DbHarness) -> dict[str, int]:
    return {
        t: int(owner_exec(test_db, f"SELECT count(*) FROM {t}").scalar_one())  # noqa: S608
        for t in COUNTED
    }


def test_reseeding_inserts_nothing_and_keeps_passwords(test_db: DbHarness) -> None:
    before = counts(test_db)
    hashes_before = owner_exec(test_db, "SELECT id, password_hash FROM users ORDER BY id").all()
    result = seed_foundation(test_db.owner_engine, test_db.foundation, environment="test")
    assert sum(result.created.values()) == 0
    assert result.new_credentials == {}
    assert counts(test_db) == before
    assert owner_exec(test_db, "SELECT id, password_hash FROM users ORDER BY id").all() == (
        hashes_before
    )
    assert before["tenants"] == 2


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_seed_refuses_outside_development(test_db: DbHarness, environment: str) -> None:
    before = counts(test_db)
    with pytest.raises(SeedRefused):
        seed_foundation(test_db.owner_engine, test_db.foundation, environment=environment)
    assert counts(test_db) == before


def test_credentials_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "var" / "dev-credentials.json"
    assert write_credentials(path, {"t": {"a@example.test": "pw"}}) == 1
    assert (path.stat().st_mode & 0o777) == 0o600
    write_credentials(path, {"t": {"b@example.test": "pw2"}})
    assert "a@example.test" in path.read_text() and "b@example.test" in path.read_text()


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def test_noop_reseed_preserves_credentials_file(test_db: DbHarness, tmp_path: Path) -> None:
    """Review finding 3: a reseed with no new users must not rewrite (or truncate) the file."""

    path = tmp_path / "dev-credentials.json"
    write_credentials(path, {"niletech": {"existing@niletech.example": "issued-earlier"}})
    before = _snapshot(path)

    result = seed_foundation(test_db.owner_engine, test_db.foundation, environment="test")
    assert result.new_credentials == {}
    assert write_credentials(path, result.new_credentials) == 0

    assert _snapshot(path) == before
    assert json.loads(path.read_text()) == {
        "niletech": {"existing@niletech.example": "issued-earlier"}
    }


@pytest.mark.parametrize("failure_point", ["json.dump", "os.replace", "os.fsync"])
def test_interrupted_replacement_keeps_previous_valid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    path = tmp_path / "dev-credentials.json"
    write_credentials(path, {"t": {"old@example.test": "old-password"}})
    before = path.read_bytes()

    module, name = failure_point.split(".")
    target: Any = {"json": json, "os": os}[module]
    original = getattr(target, name)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        if name == "dump":
            args[1].write('{"partial": ')  # simulate a half-written file
        raise OSError(f"simulated interruption in {failure_point}")

    monkeypatch.setattr(target, name, interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        write_credentials(path, {"t": {"new@example.test": "new-password"}})
    monkeypatch.setattr(target, name, original)

    assert path.read_bytes() == before
    assert json.loads(path.read_text()) == {"t": {"old@example.test": "old-password"}}
    assert [p.name for p in tmp_path.iterdir()] == ["dev-credentials.json"], "temp file left"
    assert (path.stat().st_mode & 0o777) == 0o600


def test_unreadable_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "dev-credentials.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        write_credentials(path, {"t": {"new@example.test": "pw"}})
    assert path.read_text() == "{not json"


def test_replacement_is_atomic_and_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "dev-credentials.json"
    write_credentials(path, {"t": {"a@example.test": "pw-a"}})
    first_inode = path.stat().st_ino
    write_credentials(path, {"t": {"b@example.test": "pw-b"}})
    assert path.stat().st_ino != first_inode, "file replaced, not rewritten in place"
    assert json.loads(path.read_text()) == {
        "t": {"a@example.test": "pw-a", "b@example.test": "pw-b"}
    }
    assert (path.stat().st_mode & 0o777) == 0o600
