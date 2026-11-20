"""Explicit, idempotent development seeding."""

from __future__ import annotations

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


def test_credentials_file_is_owner_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "var" / "dev-credentials.json"
    assert write_credentials(path, {"t": {"a@example.test": "pw"}}) == 1
    assert (path.stat().st_mode & 0o777) == 0o600
    write_credentials(path, {"t": {"b@example.test": "pw2"}})
    assert "a@example.test" in path.read_text() and "b@example.test" in path.read_text()
