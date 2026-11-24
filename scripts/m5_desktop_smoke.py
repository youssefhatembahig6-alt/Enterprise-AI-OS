#!/usr/bin/env python3
"""M5 desktop smoke test: the real Electron app against a real backend.

    cd backend && .venv/bin/python ../scripts/m5_desktop_smoke.py --out <report.json>

Needs the same disposable services as the integration tests (``TEST_DATABASE_ADMIN_URL``,
``TEST_REDIS_URL``, ``TEST_QDRANT_URL``; see docs/runbook.md) and the desktop app built
(``npm run build -w @eaios/web`` and ``npm run build -w @eaios/desktop``). Steps:

1. create a disposable PostgreSQL database (real migrations and seed), start the real
   FastAPI app with uvicorn on a free local port (inline dispatch, filesystem object
   store, real Qdrant, labelled fake model for embeddings);
2. run ``electron apps/desktop --smoke`` signed in as finance.reader: renderer checks,
   folder registration, the real watcher, recursion, exclusions, symlink, oversized
   file, edit/duplicate/add/delete and a simulated outage (``apps/desktop/src/smoke.ts``);
3. through the API: the owner gets a published answer citing a file from the folder,
   and a user the folder was not shared with gets the ``no_sources`` refusal.

The report combines the Electron report and the backend checks; no password or token
is written. Exit status 0 only if every step passed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT / "backend")  # alembic.ini lives here

import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from tests.integration.conftest import disposable_database, document_settings  # noqa: E402
from tests.integration.report_support import ask  # noqa: E402

FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
QUESTION = "Which train tickets need approval?"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def electron_binary() -> str:
    name = "electron.cmd" if os.name == "nt" else "electron"
    return str(ROOT / "node_modules" / ".bin" / name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    qdrant = os.environ["TEST_QDRANT_URL"]
    started = datetime.now(UTC).isoformat()
    backend_checks: list[dict[str, object]] = []
    electron_report: dict[str, object] = {}
    tmp = Path(tempfile.mkdtemp(prefix="eaios-desktop-smoke-"))
    try:
        with disposable_database(seed=True) as db:
            settings = document_settings(
                db,
                qdrant,  # readiness probe target only
                qdrant,
                str(tmp / "objects"),
                "smoke" + uuid.uuid4().hex[:8],
                CORS_ALLOWED_ORIGINS="app://eaios",
            )
            app = create_app(settings)
            port = free_port()
            server = uvicorn.Server(
                uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            while not server.started:
                time.sleep(0.05)
            base = f"http://127.0.0.1:{port}"
            tenant = db.foundation.tenants["niletech"]
            report_file = tmp / "electron-report.json"
            env = {
                **os.environ,
                "EAIOS_API_BASE_URL": base,
                "EAIOS_SMOKE_TENANT": "niletech",
                "EAIOS_SMOKE_EMAIL": tenant.users["finance-reader"].email,
                "EAIOS_SMOKE_PASSWORD": db.password(FINANCE),
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
            if report_file.exists():
                electron_report = json.loads(report_file.read_text())
            electron_report["exit_code"] = proc.returncode
            electron_report["stderr_tail"] = proc.stderr[-1500:]

            # Backend view: ingestion finished and authorization holds end to end.
            with TestClient(app) as client:

                def token(ref: str) -> str:
                    slug, key = ref.split("/", 1)
                    response = client.post(
                        "/api/v1/auth/login",
                        json={
                            "tenant": slug,
                            "email": db.foundation.tenants[slug].users[key].email,
                            "password": db.password(ref),
                        },
                    )
                    return str(response.json()["access_token"])

                deadline = time.monotonic() + 60
                owner = None
                while time.monotonic() < deadline:
                    owner = ask(client, token(FINANCE), QUESTION, mode="documents")
                    if owner.done.get("outcome") == "published":
                        break
                    time.sleep(1)
                quotes = [f.data.get("quote", "") for f in owner.of("citation")] if owner else []
                backend_checks.append(
                    {
                        "name": "owner_answer_cites_folder_file",
                        "ok": bool(owner)
                        and owner.done.get("outcome") == "published"
                        and any("50 USD" in q for q in quotes),
                        "detail": {"quotes": quotes},
                    }
                )
                other = ask(client, token(SALES), QUESTION, mode="documents")
                backend_checks.append(
                    {
                        "name": "unshared_user_gets_no_sources",
                        "ok": (other.done.get("abstention") or {}).get("code") == "no_sources",
                    }
                )
            server.should_exit = True
            thread.join(timeout=10)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    steps = list(electron_report.get("steps", []))  # type: ignore[call-overload]
    ok = (
        bool(steps)
        and all(s.get("ok") for s in steps)
        and electron_report.get("exit_code") == 0
        and all(c["ok"] for c in backend_checks)
    )
    report = {
        "kind": "m5_desktop_smoke",
        "result": "passed" if ok else "failed",
        "live": "real Electron app, real FastAPI server, real PostgreSQL and Qdrant; "
        "fake model (embeddings only) and a simulated network outage",
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "host": {"system": platform.system(), "release": platform.release(), "python": platform.python_version()},
        "electron": electron_report,
        "backend_checks": backend_checks,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"desktop smoke {report['result']}: {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
