#!/usr/bin/env python3
"""Portable developer commands (Debian and Windows). Standard library only.

    python scripts/dev.py init-env            create .env with random secrets (won't overwrite)
    python scripts/dev.py up                  docker compose up --build -d
    python scripts/dev.py seed                explicit, idempotent development seed
    python scripts/dev.py status              docker compose ps
    python scripts/dev.py down                stop containers (data volumes are kept)
    python scripts/dev.py test-backend        backend suite in Compose against real PostgreSQL/Redis
    python scripts/dev.py check               local Ruff, Mypy, unit tests, frontend lint/typecheck/build
    python scripts/dev.py lock                regenerate Python lockfiles (hashes)
    python scripts/dev.py openapi             regenerate docs/openapi.json
    python scripts/dev.py DESTROY-LOCAL-DATA --yes-delete-my-local-eaios-data
                                              DESTRUCTIVE: removes this project's containers
                                              AND volumes (all local users, tenants, sessions)

Set DOCKER_COMPOSE to override the compose command (default "docker compose").
On Debian without an active docker-group login, wrap the whole call:
    sg docker -c "python3 scripts/dev.py up"
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_BIN = BACKEND / ".venv" / ("Scripts" if os.name == "nt" else "bin")


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(shlex.quote(c) for c in cmd), flush=True)
    result = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, check=False)
    if result.returncode != 0:
        sys.exit(result.returncode)


def compose(*args: str) -> None:
    base = shlex.split(os.environ.get("DOCKER_COMPOSE", "docker compose"))
    run([*base, *args])


def tool(name: str) -> str:
    candidate = VENV_BIN / (name + (".exe" if os.name == "nt" else ""))
    return str(candidate) if candidate.exists() else name


def npm() -> str:
    return shutil.which("npm") or "npm"


def init_env() -> None:
    target = ROOT / ".env"
    if target.exists():
        print(".env already exists; leaving it unchanged")
        return
    values = {
        "POSTGRES_SUPERUSER_PASSWORD": secrets.token_urlsafe(24),
        "EAIOS_OWNER_DB_PASSWORD": secrets.token_urlsafe(24),
        "EAIOS_APP_DB_PASSWORD": secrets.token_urlsafe(24),
        "REDIS_PASSWORD": secrets.token_urlsafe(24),
        "JWT_SECRET": secrets.token_urlsafe(48),
    }
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        key = line.split("=", 1)[0]
        lines.append(f"{key}={values[key]}" if key in values else line)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    print("wrote .env with random secrets (owner-only permissions)")


def seed() -> None:
    (ROOT / "var").mkdir(exist_ok=True)
    user = []
    if hasattr(os, "getuid"):
        user = ["--user", f"{os.getuid()}:{os.getgid()}"]
    compose(
        "run", "--rm", *user,
        "-v", f"{ROOT / 'var'}:/app/var",
        "migrate", "python", "-m", "app.cli", "seed-dev",
        "--credentials-file", "/app/var/dev-credentials.json",
    )
    print(f"development credentials (if any were created): {ROOT / 'var' / 'dev-credentials.json'}")


def check() -> None:
    run([tool("ruff"), "check", "."], cwd=BACKEND)
    run([tool("ruff"), "format", "--check", "."], cwd=BACKEND)
    run([tool("mypy"), "app", "tests"], cwd=BACKEND)
    run([tool("pytest"), "-q", "tests/unit"], cwd=BACKEND)
    run([npm(), "run", "lint"])
    run([npm(), "run", "typecheck"])
    run([npm(), "run", "build"])
    compose("config", "--quiet")


def lock() -> None:
    for src, out in (("requirements.in", "requirements.lock"),
                     ("requirements-dev.in", "requirements-dev.lock")):
        run([tool("pip-compile"), "-q", "--generate-hashes", "--strip-extras", "--allow-unsafe",
             "-o", out, src], cwd=BACKEND)


def destroy(args: list[str]) -> None:
    if "--yes-delete-my-local-eaios-data" not in args:
        sys.exit("refused: this deletes all local EAIOS data volumes. Re-run with "
                 "--yes-delete-my-local-eaios-data if you really mean it.")
    compose("down", "--volumes", "--remove-orphans")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    command, rest = sys.argv[1], sys.argv[2:]
    actions = {
        "init-env": init_env,
        "up": lambda: compose("up", "--build", "-d", "--wait"),
        "seed": seed,
        "status": lambda: compose("ps"),
        "down": lambda: compose("down"),
        "test-backend": lambda: compose("--profile", "test", "run", "--rm", "--build",
                                        "backend-tests"),
        "check": check,
        "lock": lock,
        "openapi": lambda: run([tool("python"), "-m", "app.cli", "export-openapi"], cwd=BACKEND),
    }
    if command == "DESTROY-LOCAL-DATA":
        destroy(rest)
    elif command in actions:
        actions[command]()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
