#!/usr/bin/env python3
"""M5 desktop smoke test: the real Electron app against a real backend.

Two ways to provide the backend:

``--disposable`` (Linux/macOS developer machines; needs the backend venv)::

    cd backend && .venv/bin/python ../scripts/m5_desktop_smoke.py --disposable --out <report.json>

  Creates a disposable PostgreSQL database (real migrations and seed) with the
  integration-test services (``TEST_DATABASE_ADMIN_URL``, ``TEST_REDIS_URL``,
  ``TEST_QDRANT_URL``) and starts the real FastAPI app in-process (inline dispatch,
  filesystem object store, real Qdrant, labelled fake model for embeddings).

``--api URL`` (any OS, including Windows; standard library only)::

    python scripts/m5_desktop_smoke.py --api http://localhost:8000 --out <report.json>

  Uses a running stack (Docker Compose started with ``MODEL_MODE=fake`` and seeded) and
  the development credentials in ``var/dev-credentials.json``. The temporary folder
  source the smoke registers is deleted again at the end (``--keep`` keeps it).

Both need the desktop app built (``npm run build -w @eaios/web`` and
``npm run build -w @eaios/desktop``). Steps:

1. ``electron apps/desktop --smoke`` signed in as finance.reader: renderer checks, folder
   registration, the real watcher, recursion, exclusions, symlink (where the OS allows),
   oversized file, edit/duplicate/add/delete and a simulated outage
   (``apps/desktop/src/smoke.ts``);
2. through the API: the owner gets a published answer citing a file from the folder, and
   sales.reader (the folder was not shared) gets the ``no_sources`` refusal.

The report combines the Electron report and the backend checks; no password or token is
written. Exit status 0 only if every step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Which train tickets need approval?"
TENANT = "niletech"
OWNER = "finance.reader@niletech.example"
OTHER = "sales.reader@niletech.example"


def electron_binary() -> str:
    """The Electron executable (``require('electron')`` in Node returns its path)."""

    done = subprocess.run(
        ["node", "-e", "process.stdout.write(require('electron'))"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def call(
    base: str, method: str, path: str, token: str | None = None, body: Any = None
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - local API
            raw, status = response.read().decode(), response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(), exc.code
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def login(base: str, email: str, password: str) -> str:
    status, body = call(
        base,
        "POST",
        "/api/v1/auth/login",
        body={"tenant": TENANT, "email": email, "password": password},
    )
    if status != 200:
        raise SystemExit(f"login failed for {email} ({status})")
    return str(body["access_token"])


def ask(base: str, token: str, question: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310
        f"{base}/api/v1/chat",
        data=json.dumps({"question": question, "mode": "documents"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    frames: list[tuple[str, dict[str, Any]]] = []
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        kind = None
        for line in response.read().decode().splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: ") and kind:
                frames.append((kind, json.loads(line[6:])))
    done = next((d for k, d in frames if k == "done"), {})
    return {
        "outcome": done.get("outcome"),
        "abstention": (done.get("abstention") or {}).get("code"),
        "quotes": [d.get("quote", "") for k, d in frames if k == "citation"],
        "document_ids": [d.get("document_id") for k, d in frames if k == "citation"],
    }


@contextmanager
def disposable_backend() -> Iterator[tuple[str, dict[str, str]]]:
    """In-process real backend on a disposable database (backend venv required)."""

    import threading  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "backend"))
    os.chdir(ROOT / "backend")  # alembic.ini lives here
    import socket  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    from app.main import create_app  # noqa: PLC0415
    from tests.integration.conftest import disposable_database, document_settings  # noqa: PLC0415

    qdrant = os.environ["TEST_QDRANT_URL"]
    tmp = Path(tempfile.mkdtemp(prefix="eaios-desktop-smoke-"))
    try:
        with disposable_database(seed=True) as db:
            settings = document_settings(
                db, qdrant, qdrant, str(tmp / "objects"), "smoke" + uuid.uuid4().hex[:8],
                CORS_ALLOWED_ORIGINS="app://eaios",
            )  # fmt: skip
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = int(s.getsockname()[1])
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings),
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                )
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            while not server.started:
                time.sleep(0.05)
            yield (
                f"http://127.0.0.1:{port}",
                {
                    OWNER: db.password(f"{TENANT}/finance-reader"),
                    OTHER: db.password(f"{TENANT}/sales-reader"),
                },
            )
            server.should_exit = True
            thread.join(timeout=10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_smoke(
    base: str, passwords: dict[str, str], keep: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    work = Path(tempfile.mkdtemp(prefix="eaios-electron-report-"))
    report_file = work / "electron-report.json"
    env = {
        **os.environ,
        "EAIOS_API_BASE_URL": base,
        "EAIOS_SMOKE_TENANT": TENANT,
        "EAIOS_SMOKE_EMAIL": OWNER,
        "EAIOS_SMOKE_PASSWORD": passwords[OWNER],
        "EAIOS_SMOKE_REPORT": str(report_file),
    }
    proc = subprocess.run(  # noqa: S603 - local Electron binary
        [electron_binary(), str(ROOT / "apps" / "desktop"), "--smoke"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    electron: dict[str, Any] = json.loads(report_file.read_text()) if report_file.exists() else {}
    shutil.rmtree(work, ignore_errors=True)
    electron["exit_code"] = proc.returncode
    electron["stderr_tail"] = proc.stderr[-1500:]

    checks: list[dict[str, Any]] = []
    owner = login(base, OWNER, passwords[OWNER])
    deadline = time.monotonic() + 120
    result: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = ask(base, owner, QUESTION)
        if result["outcome"] == "published" and any("50 USD" in q for q in result["quotes"]):
            break
        time.sleep(2)
    checks.append(
        {
            "name": "owner_answer_cites_folder_file",
            "ok": result.get("outcome") == "published"
            and any("50 USD" in q for q in result["quotes"]),
            "detail": {"quotes": result.get("quotes")},
        }
    )
    source_id = next(
        (
            (s.get("detail") or {}).get("source_id")
            for s in electron.get("steps", [])
            if s["name"] == "register_folder"
        ),
        None,
    )
    folder_docs: set[str] = set()
    if source_id:
        _, items = call(base, "GET", f"/api/v1/local-folders/{source_id}/items", owner)
        folder_docs = {i["document_id"] for i in items or [] if i.get("document_id")}
    other = ask(base, login(base, OTHER, passwords[OTHER]), QUESTION)
    leaked = folder_docs & set(other["document_ids"]) or any("50 USD" in q for q in other["quotes"])
    checks.append(
        {
            "name": "unshared_user_never_receives_folder_content",
            "ok": bool(folder_docs) and not leaked,
            "detail": {
                "folder_documents": len(folder_docs),
                "other_outcome": other["outcome"],
                "other_abstention": other["abstention"],
                "other_cited_folder_documents": sorted(folder_docs & set(other["document_ids"])),
            },
        }
    )
    if source_id and not keep:
        status, _ = call(base, "DELETE", f"/api/v1/local-folders/{source_id}", owner)
        checks.append({"name": "cleanup_temporary_folder_source", "ok": status == 204})
    return electron, checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--disposable",
        action="store_true",
        help="in-process backend on a disposable database",
    )
    mode.add_argument("--api", help="URL of a running, seeded stack in MODEL_MODE=fake")
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary folder source (--api)"
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    started = datetime.now(UTC).isoformat()
    if args.disposable:
        with disposable_backend() as (base, passwords):
            electron, checks = run_smoke(base, passwords, keep=True)
        backend = (
            "disposable: in-process FastAPI, disposable PostgreSQL, Qdrant test service; fake model"
        )
    else:
        creds = json.loads(Path(args.credentials).read_text())[TENANT]
        base = args.api.rstrip("/")
        _, ready = call(base, "GET", "/ready")
        electron, checks = run_smoke(base, {OWNER: creds[OWNER], OTHER: creds[OTHER]}, args.keep)
        mode = (ready or {}).get("model_inference")
        backend = f"running stack at {base}; /ready model_inference={mode}"
    steps = list(electron.get("steps", []))
    ok = (
        bool(steps)
        and all(s.get("ok") for s in steps)
        and electron.get("exit_code") == 0
        and all(c["ok"] for c in checks)
    )
    report = {
        "kind": "m5_desktop_smoke",
        "result": "passed" if ok else "failed",
        "live": "real Electron app and real backend; fake model (embeddings only); "
        "simulated network outage",
        "backend": backend,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
        },
        "electron": electron,
        "backend_checks": checks,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"desktop smoke {report['result']}: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
