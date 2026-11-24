"""Dry run of the live GitHub acceptance runner (scripts/m5_github_acceptance.py).

The runner is driven end to end over HTTP against the real API (uvicorn), real
PostgreSQL, Qdrant and the M3 ingestion path, with the labelled fake model. GitHub is
the in-memory **double** on both channels: behind the connector, and behind the
runner's independent repository reader (rename, delete, collaborator removal). This
proves the runner and every stage A-E against the application; it is not live GitHub
evidence (docs/evidence/m5/README.md).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from tests.integration.m5_support import M5, FakeGitHub, m5

__all__ = ["m5"]

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "m5_github_acceptance.py"
_spec = importlib.util.spec_from_file_location("m5_github_acceptance", SCRIPT)
assert _spec and _spec.loader
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "github-acceptance"
REPO = "niletech/acceptance-fixture"
LOGIN = "second-account"


class HubRepo:
    """The runner's independent repository channel, over the same GitHub double."""

    def __init__(self, hub: FakeGitHub) -> None:
        self.hub = hub

    def read_file(self, repo: str, path: str, ref: str) -> bytes:
        assert ref == self.hub.head(repo), "the double only serves the current head"
        return self.hub.repos[repo.lower()].files[path]

    def rename(self, repo: str, path: str, new_path: str) -> None:
        files = self.hub.repos[repo.lower()].files
        files[new_path] = files.pop(path)
        self.hub.commit(repo)

    def delete(self, repo: str, path: str) -> None:
        self.hub.repos[repo.lower()].files.pop(path)
        self.hub.commit(repo)

    def is_collaborator(self, repo: str, login: str) -> bool:
        return login.lower() in {c.lower() for c in self.hub.repos[repo.lower()].collaborators}

    def remove_collaborator(self, repo: str, login: str) -> None:
        collaborators = self.hub.repos[repo.lower()].collaborators
        for key in [k for k in collaborators if k.lower() == login.lower()]:
            collaborators.pop(key)


@pytest.fixture(scope="module")
def api_url(m5: M5) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            m5.client.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_acceptance_runner_passes_every_stage(m5: M5, api_url: str, tmp_path: Path) -> None:
    repo = m5.hub.repo(REPO, collaborators={LOGIN: "pull"})
    for rel in ("inventory/reservations.py", "billing/refunds.py"):
        repo.files[rel] = (FIXTURE / rel).read_bytes()
    tenant = m5.db.foundation.tenants["niletech"]
    reader, admin = tenant.users["finance-reader"].email, tenant.users["access-admin"].email
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {
                "niletech": {
                    reader: m5.db.password("niletech/finance-reader"),
                    admin: m5.db.password("niletech/access-admin"),
                }
            }
        )
    )
    out = tmp_path / "evidence.json"
    code = runner.main(
        [
            "--api", api_url, "--repo", REPO, "--login", LOGIN, "--credentials", str(creds),
            "--out", str(out), "--poll", "0.1", "--timeout", "60", "--live-label",
            "no: in-memory GitHub double (dry run)",
        ],
        repo=HubRepo(m5.hub),
    )  # fmt: skip
    evidence: dict[str, Any] = json.loads(out.read_text())
    failed = [s for s in evidence["steps"] if not s["ok"]]
    assert code == 0 and not failed, json.dumps(failed or evidence, indent=2)[:3000]
    names = [s["step"] for s in evidence["steps"]]
    for stage in ("A_", "B_", "C_", "D_", "E_"):
        assert any(n.startswith(stage) for n in names), stage
    assert "password" not in out.read_text().lower()
    if target := os.environ.get("EAIOS_M5_DRYRUN_EVIDENCE"):
        shutil.copy(out, target)
