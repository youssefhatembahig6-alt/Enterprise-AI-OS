"""Shared M5 harness: a disposable database, the real API with inline dispatch, real
Qdrant, the fake model and an in-memory GitHub double.

``FakeGitHub`` is a **test double**: it exercises the repository sync, permissions,
revocation and deletion logic against real PostgreSQL, Qdrant and the M3 ingestion
path, but it is not evidence that the live GitHub API behaves the same way. The live
acceptance runner is ``scripts/m5_github_acceptance.py`` (docs/evidence/m5/README.md).
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.connectors.github import (
    Collaborator,
    CollaboratorsUnavailable,
    GitHubUnavailable,
    RefNotFound,
    Repository,
    RepositoryNotAccessible,
    TreeEntry,
)
from app.main import create_app
from tests.integration.conftest import (
    DbHarness,
    disposable_database,
    document_settings,
    login,
    owner_exec,
)
from tests.integration.document_support import auth

LEGAL = Path(__file__).resolve().parents[2] / "fixtures" / "legal"
LEGAL_MANIFEST = tomllib.loads((LEGAL / "legal.toml").read_text())
REPO = "niletech/inventory-service"
ALLOWED_REPOS = [REPO, "niletech/other-service", "niletech/renamed-service"]


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git id


@dataclass
class FakeRepo:
    full_name: str
    visibility: str = "private"
    default_branch: str = "main"
    files: dict[str, bytes] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)  # declared size override
    submodules: set[str] = field(default_factory=set)
    collaborators: dict[str, str] = field(default_factory=dict)  # login -> permission
    commits: int = 1


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, FakeRepo] = {}
        self.inaccessible: set[str] = set()
        self.renamed: dict[str, str] = {}
        self.collaborators_forbidden: set[str] = set()
        self.unavailable: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    # -- test controls -----------------------------------------------------------------------

    def repo(self, name: str = REPO, **kw: Any) -> FakeRepo:
        repo = FakeRepo(name, **kw)
        self.repos[name.lower()] = repo
        return repo

    def commit(self, name: str = REPO) -> None:
        self.repos[name.lower()].commits += 1

    def _get(self, name: str) -> FakeRepo:
        key = name.lower()
        if key in self.inaccessible or key not in self.repos:
            raise RepositoryNotAccessible("404")
        return self.repos[key]

    def head(self, name: str = REPO) -> str:
        repo = self.repos[name.lower()]
        state = json.dumps(
            [repo.commits, sorted((p, blob_sha(d)) for p, d in repo.files.items())]
        ).encode()
        return hashlib.sha1(state).hexdigest()  # noqa: S324 - fake commit id

    # -- GitHubClient ----------------------------------------------------------------------------

    def get_repository(self, full_name: str) -> Repository:
        with self._lock:
            self.calls.append(("get_repository", full_name))
            if full_name.lower() in self.unavailable:
                raise GitHubUnavailable("503")
            repo = self._get(full_name)
            name = self.renamed.get(full_name.lower(), repo.full_name)
            return Repository(name, repo.visibility, repo.default_branch)

    def list_collaborators(self, full_name: str) -> list[Collaborator]:
        with self._lock:
            self.calls.append(("list_collaborators", full_name))
            if full_name.lower() in self.collaborators_forbidden:
                raise CollaboratorsUnavailable("403")
            repo = self._get(full_name)
            return [Collaborator(login.lower(), perm) for login, perm in repo.collaborators.items()]

    def resolve_commit(self, full_name: str, ref: str) -> str:
        with self._lock:
            self.calls.append(("resolve_commit", full_name))
            repo = self._get(full_name)
            if ref != repo.default_branch:
                raise RefNotFound("404")
            return self.head(full_name)

    def get_tree(self, full_name: str, commit: str) -> list[TreeEntry]:
        with self._lock:
            self.calls.append(("get_tree", full_name))
            repo = self._get(full_name)
            entries = []
            dirs: set[str] = set()
            for path, data in sorted(repo.files.items()):
                parts = path.split("/")
                for i in range(1, len(parts)):
                    dirs.add("/".join(parts[:i]))
                entries.append(
                    TreeEntry(
                        path,
                        repo.modes.get(path, "100644"),
                        "blob",
                        blob_sha(data),
                        repo.sizes.get(path, len(data)),
                    )
                )
            for sub in sorted(repo.submodules):
                entries.append(TreeEntry(sub, "160000", "commit", "0" * 40, None))
            entries += [TreeEntry(d, "040000", "tree", "1" * 40, None) for d in sorted(dirs)]
            return entries

    def get_blob(self, full_name: str, sha: str) -> bytes:
        with self._lock:
            self.calls.append(("get_blob", sha))
            repo = self._get(full_name)
            for data in repo.files.values():
                if blob_sha(data) == sha:
                    return data
            raise RepositoryNotAccessible("blob 404")

    def downloads(self) -> int:
        return sum(1 for c in self.calls if c[0] == "get_blob")


class FakeGitHubFactory:
    def __init__(self) -> None:
        self.hubs: dict[str, FakeGitHub] = {}

    def __call__(self, slug: str) -> Any:
        return self.hubs.get(slug)


@dataclass
class M5:
    client: TestClient
    db: DbHarness
    hub: FakeGitHub
    settings: Settings
    config_path: Path

    def token(self, ref: str) -> str:
        return login(self.client, self.db, ref)

    def user_id(self, ref: str) -> uuid.UUID:
        slug, key = ref.split("/", 1)
        return self.db.foundation.tenants[slug].users[key].id

    def set_allowed(self, repos: list[str]) -> None:
        raw = json.loads(self.config_path.read_text())
        raw["tenants"]["niletech"]["allowed_repositories"] = repos
        self.config_path.write_text(json.dumps(raw))

    def link(self, admin: str, user: str, login_: str | None) -> Any:
        return self.client.put(
            f"/api/v1/admin/users/{self.user_id(user)}/external-accounts",
            json={"github": login_},
            headers=auth(self.token(admin)),
        )

    def create_repo_source(self, admin: str, repo: str = REPO, **kw: Any) -> dict[str, Any]:
        body = {
            "kind": "github",
            "name": kw.pop("name", repo),
            "external_root": repo,
            "department": kw.pop("department", "it"),
            "classification": kw.pop("classification", "internal"),
            "department_restricted": kw.pop("department_restricted", False),
            "allowed_countries": kw.pop("allowed_countries", ["*"]),
            **kw,
        }
        response = self.client.post(
            "/api/v1/admin/sources", json=body, headers=auth(self.token(admin))
        )
        assert response.status_code == 201, response.text
        return dict(response.json())

    def sync(self, admin: str, source_id: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/v1/admin/sources/{source_id}/sync", headers=auth(self.token(admin))
        )
        assert response.status_code == 202, response.text
        return dict(response.json())

    def item(self, source_id: str, path: str) -> Any:
        return owner_exec(
            self.db,
            "SELECT document_id, state, reason, external_sha FROM connector_items"
            " WHERE source_id = :s AND path = :p",
            {"s": source_id, "p": path},
        ).one_or_none()

    def wait_ready(self, document_id: Any, version: int = 1, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        status = "?"
        while time.monotonic() < deadline:
            with self.db.owner_engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT v.status, g.version AS active_version FROM document_versions v"
                        " JOIN documents d ON d.id = v.document_id"
                        " LEFT JOIN document_generations g ON g.document_id = d.id"
                        "   AND g.generation = d.active_generation"
                        " WHERE d.id = :d AND v.version = :v"
                    ),
                    {"d": document_id, "v": version},
                ).one()
            status = row.status
            if status == "ready" and row.active_version == version:
                return
            if status in ("failed", "needs_ocr"):
                break
            time.sleep(0.05)
        raise AssertionError(f"document {document_id} v{version} stuck in {status}")

    def epoch(self) -> int:
        return int(
            owner_exec(
                self.db, "SELECT policy_epoch FROM tenants WHERE slug = 'niletech'"
            ).scalar_one()
        )

    # -- legal fixtures ----------------------------------------------------------------------

    def upload_contract(self, key: str) -> str:
        entry = next(c for c in LEGAL_MANIFEST["contracts"] if c["key"] == key)
        tenant = self.db.foundation.tenants["niletech"]
        perms = entry["permissions"]
        grants = [
            {"effect": g["effect"], "group": g["group"]}
            if "group" in g
            else {"effect": g["effect"], "user_email": tenant.users[g["user"]].email}
            for g in perms["grants"]
        ]
        body = {
            "filename": entry["file"],
            "content_base64": base64.b64encode(
                (LEGAL / "files" / entry["file"]).read_bytes()
            ).decode(),
            "department": entry["department"],
            "title": entry["title"],
            "permissions": {
                "classification": perms["classification"],
                "department_restricted": perms["department_restricted"],
                "allowed_countries": perms["allowed_countries"],
                "grants": grants,
            },
        }
        response = self.client.post(
            "/api/v1/documents", json=body, headers=auth(self.token("niletech/access-admin"))
        )
        assert response.status_code == 202, response.text
        document_id = str(response.json()["document_id"])
        self.wait_ready(document_id)
        return document_id


def build_m5(stub_http_url: str, qdrant_url: str, tmp: Path, **overrides: Any) -> Iterator[M5]:
    config = tmp / "github-connector.json"
    config.write_text(
        json.dumps(
            {
                "tenants": {
                    "niletech": {
                        # Never used: tests inject the in-memory factory below.
                        "auth": {"type": "token", "token": "unused-test-value"},
                        "allowed_repositories": ALLOWED_REPOS,
                    }
                }
            }
        )
    )
    with disposable_database(seed=True) as db:
        prefix = "m5" + uuid.uuid4().hex[:8]
        settings = document_settings(
            db,
            stub_http_url,
            qdrant_url,
            str(tmp / "objects"),
            prefix,
            GITHUB_CONNECTOR_CONFIG=str(config),
            **overrides,
        )
        factory = FakeGitHubFactory()
        hub = FakeGitHub()
        factory.hubs["niletech"] = hub
        with TestClient(create_app(settings, github_clients=factory)) as client:
            yield M5(client, db, hub, settings, config)
            client.app.state.documents._qdrant.delete_collection(  # type: ignore[attr-defined]
                client.app.state.report_runner.documents_workflow.collection()  # type: ignore[attr-defined]
            )


@pytest.fixture(scope="module")
def m5(
    stub_http_url: str, qdrant_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[M5]:
    yield from build_m5(stub_http_url, qdrant_url, tmp_path_factory.mktemp("m5"))
