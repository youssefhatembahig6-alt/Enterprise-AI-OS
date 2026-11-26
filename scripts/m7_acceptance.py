#!/usr/bin/env python3
"""M7 functional acceptance: a disposable Compose stack next to the owner's own stack.

Standard library only. Every command passes ``-p eaios-m7acc -f compose.yaml -f
infra/acceptance/compose.m7.yaml --env-file var/m7-acceptance/.env``, so the stack has
its own project name, volumes, image tags and ports:
web http://localhost:13000, API http://localhost:18000, Mailpit http://localhost:18025.
The project cannot be redirected: COMPOSE_PROJECT_NAME and the other COMPOSE_* path
variables are dropped from child processes, the passthrough refuses global -p/-f/
--env-file/--project-directory, and a read-only ``docker compose config`` check that the
project is eaios-m7acc (with only eaios-m7acc_* volumes) runs before every Compose call.

    python3 scripts/m7_acceptance.py init-env   # var/m7-acceptance/.env, MODEL_MODE=fake (0600)
    python3 scripts/m7_acceptance.py up         # build + start + wait
    python3 scripts/m7_acceptance.py seed       # users, business data, M3 corpus
    python3 scripts/m7_acceptance.py seed-legal # the three M5 legal fixture contracts
    python3 scripts/m7_acceptance.py runners    # rerun the M2/M3/M6/smoke/desktop runners
    python3 scripts/m7_acceptance.py compose -- <docker compose args>
    python3 scripts/m7_acceptance.py destroy    # removes ONLY eaios-m7acc containers+volumes
    python3 scripts/m7_acceptance.py check-project  # read-only: show the resolved project

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
import urllib.error
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


# Compose variables that could point this wrapper at another project, file set or env
# file (for example an inherited COMPOSE_PROJECT_NAME=eaios). They are removed from the
# environment of every child process; the project is always given with -p.
_COMPOSE_REDIRECTS = (
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_ENV_FILES",
    "COMPOSE_PROJECT_DIRECTORY",
    "COMPOSE_PATH_SEPARATOR",
)
# Global ``docker compose`` options that would change which project or files are used.
_FORBIDDEN_GLOBALS = ("-p", "--project-name", "-f", "--file", "--env-file", "--project-directory")
# Global options that take a separate value (so the value is not the subcommand).
_VALUE_GLOBALS = (*_FORBIDDEN_GLOBALS, "--profile", "--ansi", "--progress", "--parallel")


def child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _COMPOSE_REDIRECTS}


def forbidden_globals(args: list[str]) -> list[str]:
    """Global options before the subcommand that would change the project or files.

    Only the global section is checked: after the subcommand, ``-p`` means something
    else (``docker compose run -p 8080:80`` publishes a port).
    """
    bad: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            break  # the subcommand
        name = arg.split("=", 1)[0]
        short = next((f for f in ("-p", "-f") if arg.startswith(f) and len(arg) > 2), None)
        if name in _FORBIDDEN_GLOBALS or short:
            bad.append(arg)
        i += 2 if name in _VALUE_GLOBALS and "=" not in arg and not short else 1
    return bad


def compose_cmd() -> list[str]:
    cmd = shlex.split(os.environ.get("DOCKER_COMPOSE", "docker compose"))
    bad = forbidden_globals(cmd[2:] if cmd[:2] == ["docker", "compose"] else cmd[1:])
    if bad:
        sys.exit(f"DOCKER_COMPOSE must not set the project or files: {bad}")
    return cmd


def base() -> list[str]:
    return [
        *compose_cmd(),
        "-p",
        PROJECT,
        "-f",
        str(ROOT / "compose.yaml"),
        "-f",
        str(ROOT / "infra" / "acceptance" / "compose.m7.yaml"),
        "--env-file",
        str(ENV),
    ]


def resolved_project() -> tuple[str, list[str]]:
    """Read-only: the project name and volume names Compose resolves for base()."""
    done = subprocess.run(  # noqa: S603 - fixed argv
        [*base(), "config", "--format", "json"],
        cwd=ROOT,
        env=child_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        sys.exit(f"docker compose config failed:\n{done.stderr[-2000:]}")
    model = json.loads(done.stdout)
    volumes = sorted(str(v.get("name", "")) for v in model.get("volumes", {}).values())
    return str(model.get("name", "")), volumes


def assert_disposable() -> None:
    name, volumes = resolved_project()
    stray = [v for v in volumes if not v.startswith(f"{PROJECT}_")]
    if name != PROJECT or stray:
        sys.exit(f"refusing: Compose resolves project {name!r}, volumes {stray or volumes}")


def run(cmd: list[str], check: bool = True) -> int:
    print("+", " ".join(shlex.quote(c) for c in cmd), flush=True)
    if cmd[: len(base())] == base():
        assert_disposable()  # every Compose call: read-only check before it runs
    code = subprocess.run(cmd, cwd=ROOT, env=child_env(), check=False).returncode  # noqa: S603
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


IN_PROGRESS = ("queued", "parsing", "indexing")


def legal_fixture_state(expected: list[str], docs: list[dict]) -> tuple[list[str], list[str]]:
    """Classify the expected fixture titles against a GET /api/v1/documents listing.

    Returns (waiting, problems). A title is done only when exactly one document has
    that title and it is ``ready`` with ``searchable: true``. Still ingesting, or
    ``ready`` but not yet searchable (bug B2: a listing can briefly show that right
    after the ingestion commit), is ``waiting``. Missing, duplicated, or any other
    status (``failed``, ``needs_ocr``, ...) is a ``problem``.
    """
    waiting: list[str] = []
    problems: list[str] = []
    for title in expected:
        found = [d for d in docs if d.get("title") == title]
        if not found:
            problems.append(f"missing: {title}")
        elif len(found) > 1:
            problems.append(f"duplicated ({len(found)} documents): {title}")
        else:
            d = found[0]
            status, searchable = d.get("status"), d.get("searchable") is True
            if status == "ready" and searchable:
                continue
            if status in IN_PROGRESS or (status == "ready" and not searchable):
                waiting.append(f"{status}{'' if searchable else ' (not searchable yet)'}: {title}")
            else:
                error = d.get("error") or {}
                detail = f" ({error.get('code')})" if error.get("code") else ""
                problems.append(f"{status}{detail}: {title}")
    return waiting, problems


def seed_legal(timeout: float = 180.0) -> None:
    """Upload the three M5 legal fixtures with the permissions in legal.toml (API).

    Succeeds only when every expected contract is present exactly once, ``ready`` and
    ``searchable``; otherwise exits non-zero naming each missing or failed fixture.
    """
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
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - local stack
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            sys.exit(f"seed-legal: {method} {path} failed: HTTP {exc.code} {exc.read()[:300]!r}")

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
            print(f"skip   {c['key']} (already uploaded; its state is checked below)")
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
    expected = [c["title"] for c in spec["contracts"]]
    deadline = time.monotonic() + timeout
    while True:
        docs = call("GET", "/api/v1/documents", token)
        waiting, problems = legal_fixture_state(expected, docs)  # type: ignore[arg-type]
        if problems:
            sys.exit("seed-legal FAILED:\n  " + "\n  ".join(problems + waiting))
        if not waiting:
            for title in expected:
                print(f"  ready      searchable=True  {title}")
            print(f"seed-legal: all {len(expected)} legal fixtures ready and searchable")
            return
        if time.monotonic() > deadline:
            sys.exit(
                f"seed-legal FAILED: not ready after {timeout:.0f} s:\n  " + "\n  ".join(waiting)
            )
        time.sleep(2)


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
    sub.add_parser("check-project")
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
        run([*base(), "down", "--volumes", "--remove-orphans"])
    elif args.cmd == "check-project":
        name, volumes = resolved_project()
        print(f"project {name}; volumes {volumes}")
        assert_disposable()
        print("ok: pinned to the disposable project")
    elif args.cmd == "compose":
        extra = args.args[1:] if args.args[:1] == ["--"] else args.args
        bad = forbidden_globals(extra)
        if bad:
            sys.exit(f"refusing: the passthrough must not set the project or files: {bad}")
        run([*base(), *extra])


if __name__ == "__main__":
    main()
