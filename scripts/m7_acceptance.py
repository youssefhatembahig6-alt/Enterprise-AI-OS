#!/usr/bin/env python3
"""M7 functional acceptance: a disposable Compose stack next to the owner's own stack.

Standard library only. Every command passes ``-f compose.yaml -f
infra/acceptance/compose.m7.yaml --env-file var/m7-acceptance/.env``, so the stack has
its own project name (``eaios-m7acc``), volumes, image tags and ports:
web http://localhost:13000, API http://localhost:18000, Mailpit http://localhost:18025.

    python3 scripts/m7_acceptance.py init-env   # var/m7-acceptance/.env, MODEL_MODE=fake (0600)
    python3 scripts/m7_acceptance.py up         # build + start + wait
    python3 scripts/m7_acceptance.py seed       # users, business data, M3 corpus
    python3 scripts/m7_acceptance.py seed-legal # the three M5 legal fixture contracts
    python3 scripts/m7_acceptance.py runners    # rerun the M2/M3/M6/smoke/desktop runners
    python3 scripts/m7_acceptance.py compose -- <docker compose args>
    python3 scripts/m7_acceptance.py destroy    # removes ONLY eaios-m7acc containers+volumes

Fixture passwords go to var/m7-acceptance/dev-credentials.json (0600), never stdout.
Runner evidence goes to var/m7-acceptance/.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "var" / "m7-acceptance"
ENV = STATE / ".env"
CREDS = STATE / "dev-credentials.json"
API = "http://localhost:18000"
WEB = "http://localhost:13000"
MAILPIT = "http://localhost:18025"
PROJECT = "eaios-m7acc"


def base() -> list[str]:
    return [
        *shlex.split(os.environ.get("DOCKER_COMPOSE", "docker compose")),
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "infra" / "acceptance" / "compose.m7.yaml"),
        "--env-file",
        str(ENV),
    ]


def run(cmd: list[str], check: bool = True) -> int:
    print("+", " ".join(shlex.quote(c) for c in cmd), flush=True)
    code = subprocess.run(cmd, cwd=ROOT, check=False).returncode  # noqa: S603 - fixed argv
    if check and code:
        sys.exit(code)
    return code


def init_env(model_mode: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    if ENV.exists():
        print(f"{ENV} exists; leaving it unchanged")
        return
    values = {
        k: secrets.token_urlsafe(24)
        for k in (
            "POSTGRES_SUPERUSER_PASSWORD",
            "EAIOS_OWNER_DB_PASSWORD",
            "EAIOS_APP_DB_PASSWORD",
            "EAIOS_REPORT_DB_PASSWORD",
            "REDIS_PASSWORD",
        )
    }
    values |= {
        "JWT_SECRET": secrets.token_urlsafe(48),
        "MODEL_MODE": model_mode,
        "CORS_ALLOWED_ORIGINS": f"{WEB},http://127.0.0.1:13000,app://eaios",
        "VITE_API_BASE_URL": API,
        "DRIVE_CONNECTOR_CONFIG": "",
        "GITHUB_CONNECTOR_CONFIG": "",
    }
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={values[key]}" if key in values and not line.startswith("#") else line)
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"wrote {ENV} (MODEL_MODE={model_mode})")


def seed() -> None:
    user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    vol = ["-v", f"{STATE}:/app/var"]
    run(
        [
            *base(),
            "run",
            "--rm",
            *user,
            *vol,
            "migrate",
            "python",
            "-m",
            "app.cli",
            "seed-dev",
            "--credentials-file",
            "/app/var/dev-credentials.json",
        ]
    )
    run([*base(), "run", "--rm", "migrate", "python", "-m", "app.cli", "import-business-data"])
    run([sys.executable, "scripts/m3_demo.py", "seed", "--api", API, "--credentials", str(CREDS)])


def seed_legal() -> None:
    """Upload the three M5 legal fixtures with the permissions in legal.toml (API)."""
    fixtures = ROOT / "backend" / "fixtures" / "legal"
    spec = tomllib.loads((fixtures / "legal.toml").read_text())
    creds = json.loads(CREDS.read_text())["niletech"]
    emails = {"board-member": "board.member@niletech.example"}

    def call(method: str, path: str, token: str | None = None, body: object = None) -> object:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)  # noqa: S310
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - local stack
            return json.loads(resp.read() or b"null")

    admin = "access.admin@niletech.example"
    token = call(
        "POST",
        "/api/v1/auth/login",
        body={"tenant": "niletech", "email": admin, "password": creds[admin]},
    )
    token = token["access_token"]  # type: ignore[index]
    existing = {d["title"] for d in call("GET", "/api/v1/documents", token)}  # type: ignore[union-attr]
    for c in spec["contracts"]:
        if c["title"] in existing:
            print(f"skip   {c['key']} (already uploaded)")
            continue
        perms = c["permissions"]
        body = {
            "filename": c["file"],
            "title": c["title"],
            "department": c["department"],
            "content_base64": base64.b64encode(
                (fixtures / "files" / c["file"]).read_bytes()
            ).decode(),
            "permissions": {
                "classification": perms["classification"],
                "department_restricted": perms["department_restricted"],
                "allowed_countries": perms["allowed_countries"],
                "grants": [
                    {"effect": g["effect"], "group": g["group"]}
                    if "group" in g
                    else {"effect": g["effect"], "user_email": emails[g["user"]]}
                    for g in perms["grants"]
                ],
            },
        }
        result = call("POST", "/api/v1/documents", token, body)
        print(f"upload {c['key']} -> {result['document_id']}")  # type: ignore[index]
    titles = {c["title"] for c in spec["contracts"]}
    for _ in range(60):
        docs = [d for d in call("GET", "/api/v1/documents", token) if d["title"] in titles]  # type: ignore[union-attr]
        if docs and all(d["status"] not in ("queued", "parsing", "indexing") for d in docs):
            for d in docs:
                print(f"  {d['status']:<10} searchable={d['searchable']!s:<5} {d['title']}")
            return
        time.sleep(2)
    sys.exit("legal fixtures still ingesting")


def runners() -> None:
    """Existing runners against the disposable stack; each exit code is reported."""
    py = sys.executable
    steps = {
        "smoke_stack": [
            py,
            "scripts/smoke_stack.py",
            "--api",
            API,
            "--web",
            WEB,
            "--origin",
            WEB,
            "--credentials",
            str(CREDS),
        ],
        "m2_demo_capture": [
            py,
            "scripts/m2_demo_capture.py",
            "--api",
            API,
            "--credentials",
            str(CREDS),
        ],
        "m3_demo_capture": [
            py,
            "scripts/m3_demo.py",
            "capture",
            "--api",
            API,
            "--credentials",
            str(CREDS),
        ],
        "m6_demo": [
            py,
            "scripts/m6_demo.py",
            "--api",
            API,
            "--mailpit",
            MAILPIT,
            "--credentials",
            str(CREDS),
        ],
        "m5_desktop_smoke": [
            py,
            "scripts/m5_desktop_smoke.py",
            "--api",
            API,
            "--credentials",
            str(CREDS),
            "--out",
            str(STATE / "m5-desktop-smoke.json"),
        ],
    }
    results = {name: run(cmd, check=False) for name, cmd in steps.items()}
    for name, code in results.items():
        print(f"{name}: {'PASS' if code == 0 else f'FAIL (exit {code})'}")
    sys.exit(1 if any(results.values()) else 0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    env = sub.add_parser("init-env")
    env.add_argument("--model-mode", choices=("fake", "gateway"), default="fake")
    sub.add_parser("up")
    sub.add_parser("seed")
    sub.add_parser("seed-legal")
    sub.add_parser("runners")
    sub.add_parser("down")
    sub.add_parser("destroy")
    raw = sub.add_parser("compose")
    raw.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.cmd == "init-env":
        init_env(args.model_mode)
        return
    if not ENV.exists():
        sys.exit(f"{ENV} is missing; run init-env first")
    if args.cmd == "up":
        run([*base(), "up", "--build", "-d", "--wait"])
    elif args.cmd == "seed":
        seed()
    elif args.cmd == "seed-legal":
        seed_legal()
    elif args.cmd == "runners":
        runners()
    elif args.cmd == "down":
        run([*base(), "down"])
    elif args.cmd == "destroy":
        run([*base(), "-p", PROJECT, "down", "--volumes", "--remove-orphans"])
    elif args.cmd == "compose":
        extra = args.args[1:] if args.args[:1] == ["--"] else args.args
        run([*base(), *extra])


if __name__ == "__main__":
    main()
