#!/usr/bin/env python3
"""Connect the disposable chat stack (eaios-m7acc: web :13000, API :18000) to a model
gateway, or back to the labelled fake model. Standard library only.

    python3 scripts/m8_connect_gateway.py connect --url https://<gateway host>
    python3 scripts/m8_connect_gateway.py reindex      # once per embedding space (see below)
    python3 scripts/m8_connect_gateway.py status
    python3 scripts/m8_connect_gateway.py disconnect   # back to MODEL_MODE=fake

``connect`` reads the gateway token from ``EAIOS_GATEWAY_TOKEN`` or asks for it without
echo, checks the gateway's public ``/health`` (pinned model) and one authenticated call,
then writes ``MODEL_MODE=gateway``, ``LLM_BASE_URL``, ``MODEL_GATEWAY_TOKEN`` and the
longer remote timeouts into ``var/m7-acceptance/.env`` (0600, git-ignored), restarts only
the disposable API, worker and scheduler (pinned to ``-p eaios-m7acc`` by
``m7_acceptance.py``; the owner's ``eaios`` stack is never touched), and finally asks the
backend's own ``GET /api/v1/model/status?refresh=true``, the check behind the chat
header's status button. The token is never printed and never reaches the browser.

``reindex``: documents are embedded per embedding space. Documents indexed with the fake
model are not searchable with BGE-M3 (and the other way round), so after switching modes
run ``reindex`` once; it re-embeds every document through the current model.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "var" / "m7-acceptance"
ENV = STATE / ".env"
CREDS = STATE / "dev-credentials.json"
API = "http://localhost:18000"
ADMIN = "access.admin@niletech.example"
PINNED = ("Qwen/Qwen3-8B-AWQ", "4da05a8edb55c6046cce958586c33b61da07bb79")
REMOTE_TIMEOUTS = {"MODEL_READ_TIMEOUT_SECONDS": "180", "MODEL_CALL_DEADLINE_SECONDS": "360"}


def http(
    method: str, url: str, token: str | None = None, body: Any = None, timeout: float = 30
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "eaios-connect"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


def gateway_root(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        sys.exit("--url must be an http(s) URL of the gateway root")
    if parts.username or parts.password or parts.query or parts.fragment:
        sys.exit("--url must not contain credentials, a query or a fragment")
    root = url.strip().rstrip("/")
    if root.endswith("/v1"):
        sys.exit("--url is the gateway ROOT (without /v1)")
    return root


def read_env() -> list[str]:
    if not ENV.exists():
        sys.exit(f"{ENV} is missing; run python3 scripts/m7_acceptance.py init-env first")
    return ENV.read_text().splitlines()


def write_env(values: dict[str, str]) -> None:
    lines = read_env()
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in values and not line.startswith("#"):
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    fd = os.open(ENV, os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)


def restart() -> None:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "m7_acceptance.py"),
        "compose",
        "--",
        "up",
        "-d",
        "--no-deps",
        "--wait",
        "api",
        "worker",
        "scheduler",
    ]
    print("restarting the disposable API, worker and scheduler (eaios-m7acc only)…", flush=True)
    done = subprocess.run(cmd, cwd=ROOT, check=False, capture_output=True, text=True)  # noqa: S603
    if done.returncode:
        sys.exit(f"restart failed:\n{done.stdout[-1500:]}{done.stderr[-1500:]}")


def admin_token() -> str:
    creds = json.loads(CREDS.read_text())["niletech"]
    status, body = http(
        "POST",
        f"{API}/api/v1/auth/login",
        body={"tenant": "niletech", "email": ADMIN, "password": creds[ADMIN]},
    )
    if status != 200:
        sys.exit(f"could not sign in to the disposable API as {ADMIN} (HTTP {status})")
    return str(body["access_token"])


def backend_status() -> dict[str, Any]:
    for _ in range(60):
        try:
            if http("GET", f"{API}/health")[0] == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        sys.exit("the disposable API did not come up on :18000")
    status, body = http("GET", f"{API}/api/v1/model/status?refresh=true", admin_token(), timeout=60)
    if status != 200:
        sys.exit(f"model status failed (HTTP {status})")
    return dict(body)


def describe(s: dict[str, Any]) -> str:
    if s.get("mode") == "fake":
        return "FAKE model (development double)"
    chat = s.get("chat") or {}
    if s.get("verified") and s.get("authenticated"):
        return f"Connected · {chat.get('model')}@{chat.get('revision')}"
    return f"Model offline ({s.get('error')})"


def connect(url: str) -> None:
    root = gateway_root(url)
    token = os.environ.get("EAIOS_GATEWAY_TOKEN") or getpass.getpass("Gateway token: ")
    if len(token) < 16:
        sys.exit("the gateway token looks too short")
    try:
        status, health = http("GET", f"{root}/health", timeout=20)
    except OSError as exc:
        sys.exit(f"gateway unreachable from the laptop: {type(exc).__name__}")
    chat = ((health or {}).get("models") or {}).get("chat") or {}
    if status != 200 or (chat.get("model"), chat.get("revision")) != PINNED:
        sys.exit(
            f"gateway /health is not serving the pinned {PINNED[0]}@{PINNED[1][:7]} "
            f"(HTTP {status}, chat {chat})"
        )
    status, _ = http(
        "POST",
        f"{root}/v1/embeddings",
        token,
        {"model": "BAAI/bge-m3", "input": ["eaios connection check"]},
        timeout=60,
    )
    if status in (401, 403):
        sys.exit("the gateway rejected the token (Colab restarted with another token?)")
    if status != 200:
        sys.exit(f"authenticated gateway call failed (HTTP {status})")
    print(f"gateway OK from the laptop: {chat['model']}@{chat['revision'][:7]}, token accepted")
    write_env(
        {
            "MODEL_MODE": "gateway",
            "LLM_BASE_URL": root,
            "MODEL_GATEWAY_TOKEN": token,
            **REMOTE_TIMEOUTS,
        }
    )
    restart()
    state = backend_status()
    print("backend model status:", describe(state))
    if not (state.get("verified") and state.get("authenticated")):
        sys.exit(1)
    print(
        "Open http://localhost:13000 and sign in. Run `reindex` once if Documents answers "
        "say nothing was found (documents are still in the fake embedding space)."
    )


def disconnect() -> None:
    write_env({"MODEL_MODE": "fake", "LLM_BASE_URL": "", "MODEL_GATEWAY_TOKEN": ""})
    restart()
    print("backend model status:", describe(backend_status()))


def reindex(timeout: float = 1800) -> None:
    token = admin_token()
    status, docs = http("GET", f"{API}/api/v1/documents", token)
    if status != 200:
        sys.exit(f"could not list documents (HTTP {status})")
    targets = [d for d in docs if d.get("can_administer")]
    for d in targets:
        code, _ = http("POST", f"{API}/api/v1/documents/{d['document_id']}/reindex", token)
        print(f"reindex {d['title']}: HTTP {code}")
    deadline = time.monotonic() + timeout
    wanted = {d["document_id"] for d in targets}
    while time.monotonic() < deadline:
        _, docs = http("GET", f"{API}/api/v1/documents", token)
        pending = [
            d["title"]
            for d in docs
            if d["document_id"] in wanted and d["status"] in ("queued", "parsing", "indexing")
        ]
        if not pending:
            for d in docs:
                if d["document_id"] in wanted:
                    print(f"  {d['status']:<10} searchable={d['searchable']!s:<5} {d['title']}")
            return
        print(f"waiting for {len(pending)} document(s)…", flush=True)
        time.sleep(5)
    sys.exit("reindex did not finish in time")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("--url", required=True, help="gateway root, e.g. https://<host> (no /v1)")
    sub.add_parser("disconnect")
    sub.add_parser("status")
    sub.add_parser("reindex")
    args = ap.parse_args()
    if args.cmd == "connect":
        connect(args.url)
    elif args.cmd == "disconnect":
        disconnect()
    elif args.cmd == "reindex":
        reindex()
    else:
        print(describe(backend_status()))


if __name__ == "__main__":
    main()
