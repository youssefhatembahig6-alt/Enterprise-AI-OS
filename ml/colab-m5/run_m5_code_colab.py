"""M5 real-model code-answer acceptance in one Colab T4 runtime, everything over loopback.

Reuses the M3 Colab setup (``ml/colab-m3/run_m3_colab.py``: PostgreSQL 17, Redis,
Qdrant v1.15.4, the hash-locked backend venv, migrate, seed) next to the model gateway
(Qwen3-8B-AWQ, BGE-M3, the development checker) on 127.0.0.1:8100. No tunnel is opened.

    python run_m5_code_colab.py vllm-env    # M1's pinned vLLM venv (once per runtime)
    # launch the gateway (README, cell 3; Docling is not needed)
    python run_m5_code_colab.py all         # services, backend, acceptance run, bundle

The acceptance run is ``scripts/m5_github_acceptance.py`` stages A-E with
``--require-gateway``: every code answer must come from the real model (gateway mode)
and every code citation is checked against the fixture file bytes at the cited commit
(repository, path, commit, blob, lines, exact quote). GitHub is an in-memory double
holding ``backend/fixtures/github-acceptance`` (the live-GitHub run needs the test
repository; docs/evidence/m5/OPEN-ITEMS-SETUP.md). Output:
``/content/m5-evidence-<stamp>.zip`` after a secret scan.

``inner`` is the in-process part (run by ``all`` under the backend venv). Locally it can
be dry-run in fake mode with ``EAIOS_M5_REQUIRE_GATEWAY=0`` and an ``app_env`` of your own.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("M3_RUN_DIR", "/content/m5-run")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "colab-m3"))
import run_m3_colab as m3  # noqa: E402

REPO_NAME = "niletech/acceptance-fixture"
LOGIN = "second-account"
FIXTURE = m3.REPO / "backend" / "fixtures" / "github-acceptance"
FILES = ("inventory/reservations.py", "billing/refunds.py")


# -- in-process part (backend venv) ------------------------------------------------------------


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git id


class Hub:
    """Minimal GitHub double for one repository (same contract as the integration tests')."""

    def __init__(self) -> None:
        self.files = {rel: (FIXTURE / rel).read_bytes() for rel in FILES}
        self.collaborators = {LOGIN: "pull"}
        self.commits = 1

    def head(self) -> str:
        state = json.dumps([self.commits, sorted((p, _blob(d)) for p, d in self.files.items())])
        return hashlib.sha1(state.encode()).hexdigest()  # noqa: S324 - fake commit id

    # GitHubClient
    def get_repository(self, full_name: str) -> Any:
        from app.connectors.github import Repository  # noqa: PLC0415

        return Repository(REPO_NAME, "private", "main")

    def list_collaborators(self, full_name: str) -> Any:
        from app.connectors.github import Collaborator  # noqa: PLC0415

        return [Collaborator(k, v) for k, v in self.collaborators.items()]

    def resolve_commit(self, full_name: str, ref: str) -> str:
        return self.head()

    def get_tree(self, full_name: str, commit: str) -> Any:
        from app.connectors.github import TreeEntry  # noqa: PLC0415

        return [
            TreeEntry(p, "100644", "blob", _blob(d), len(d)) for p, d in sorted(self.files.items())
        ]

    def get_blob(self, full_name: str, sha: str) -> bytes:
        return next(d for d in self.files.values() if _blob(d) == sha)

    # the runner's independent channel
    def read_file(self, repo: str, path: str, ref: str) -> bytes:
        if ref != self.head():
            raise KeyError("the double only serves the current head")
        return self.files[path]

    def rename(self, repo: str, path: str, new_path: str) -> None:
        self.files[new_path] = self.files.pop(path)
        self.commits += 1

    def delete(self, repo: str, path: str) -> None:
        self.files.pop(path)
        self.commits += 1

    def is_collaborator(self, repo: str, login: str) -> bool:
        return login in self.collaborators

    def remove_collaborator(self, repo: str, login: str) -> None:
        self.collaborators.pop(login, None)


def inner(out: Path, credentials: Path) -> int:
    import uvicorn  # noqa: PLC0415

    sys.path.insert(0, str(m3.REPO / "backend"))
    from app.config import Settings  # noqa: PLC0415
    from app.main import create_app  # noqa: PLC0415

    config = out.parent / "github-connector.json"
    config.write_text(
        json.dumps(
            {
                "tenants": {
                    "niletech": {
                        "auth": {"type": "token", "token": "unused-in-memory-double"},
                        "allowed_repositories": [REPO_NAME],
                    }
                }
            }
        )
    )
    os.environ["GITHUB_CONNECTOR_CONFIG"] = str(config)
    hub = Hub()
    app = create_app(Settings(), github_clients=lambda slug: hub if slug == "niletech" else None)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    sys.path.insert(0, str(m3.REPO / "scripts"))
    import m5_github_acceptance as runner  # noqa: PLC0415

    argv = [
        "--api", f"http://127.0.0.1:{port}", "--repo", REPO_NAME, "--login", LOGIN,
        "--credentials", str(credentials), "--out", str(out), "--timeout", "3600",
        "--live-label", "no: in-memory GitHub double; real model via the Colab gateway",
    ]  # fmt: skip
    if os.environ.get("EAIOS_M5_REQUIRE_GATEWAY", "1") == "1":
        argv.append("--require-gateway")
    code = int(runner.main(argv, repo=hub))
    server.should_exit = True
    return code


# -- Colab orchestration ---------------------------------------------------------------------


def bundle(files: list[Path]) -> Path:
    needles = [p.read_text() for p in m3.SECRETS.iterdir() if p.is_file() and p.suffix == ""]
    needles.append(os.environ["GATEWAY_TOKEN"])
    creds = json.loads((m3.SECRETS / "dev-credentials.json").read_text())
    needles += [pw for users in creds.values() if isinstance(users, dict) for pw in users.values()]
    leaks = [f.name for f in files if any(n and n in f.read_text(errors="ignore") for n in needles)]
    if leaks:
        raise SystemExit(f"secret scan failed; not bundling: {leaks}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"/content/m5-evidence-{stamp}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"m5-evidence-{stamp}/{f.name}")
    print(f"secret scan clean; wrote {target}")
    return target


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step == "vllm-env":
        m3.vllm_env()
        return
    if step == "inner":
        sys.exit(inner(Path(sys.argv[2]), Path(sys.argv[3])))
    for d in (m3.OUT, m3.LOGS):
        d.mkdir(parents=True, exist_ok=True)
    health = m3.get_json(f"{m3.GATEWAY}/health")
    (m3.OUT / "gateway-health.json").write_text(json.dumps(health, indent=2) + "\n")
    Path("/content/m3-run/logs").mkdir(parents=True, exist_ok=True)  # M3 helper's Qdrant log path
    m3.services()
    m3.backend()
    sha = m3.sh("git rev-parse HEAD", cwd=m3.REPO).stdout.strip()
    out = m3.OUT / "m5-code-real-model.json"
    done = subprocess.run(  # noqa: S603 - the backend venv runs the in-process part
        [
            f"{m3.VENV}/bin/python",
            __file__,
            "inner",
            str(out),
            str(m3.SECRETS / "dev-credentials.json"),
        ],
        cwd=m3.REPO / "backend",
        env=m3.app_env(),
        check=False,
    )
    (m3.OUT / "versions.json").write_text(
        json.dumps({"application_commit": sha, "exit_code": done.returncode}, indent=2) + "\n"
    )
    bundle([out, m3.OUT / "gateway-health.json", m3.OUT / "versions.json"])
    if done.returncode != 0:
        raise SystemExit(f"acceptance run failed ({done.returncode}); see {out}")


if __name__ == "__main__":
    main()
