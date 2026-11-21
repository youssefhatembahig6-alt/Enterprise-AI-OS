#!/usr/bin/env python3
"""End-to-end smoke check against a running Compose stack (standard library only).

    python scripts/smoke_stack.py [--api http://127.0.0.1:8000] [--web http://127.0.0.1:3000]

Uses the development credentials written by `python scripts/dev.py seed`. Never
prints passwords or tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return raw.decode(errors="replace")


def call(method: str, url: str, body: Any = None, token: str | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, _json(raw), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, _json(raw), {k.lower(): v for k, v in err.headers.items()}


def check(label: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--web", default="http://127.0.0.1:3000")
    parser.add_argument("--credentials", default=str(ROOT / "var/dev-credentials.json"))
    args = parser.parse_args()

    status, body, _ = call("GET", f"{args.api}/health")
    check("liveness 200", status == 200 and body == {"status": "ok"})
    status, body, _ = call("GET", f"{args.api}/ready")
    check(f"readiness 200 ({body})", status == 200 and body["status"] == "ready")
    check("no model capability claimed", body["model_inference"] != "available")

    with urllib.request.urlopen(args.web, timeout=10) as resp:
        check("frontend serves index", resp.status == 200 and b'id="root"' in resp.read())

    creds = json.loads(Path(args.credentials).read_text())
    tenant = "niletech"
    email = "finance.reader@niletech.example"
    password = creds[tenant][email]

    status, body, _ = call("POST", f"{args.api}/api/v1/auth/login",
                           {"tenant": tenant, "email": email, "password": "wrong-" + password})
    check("wrong password 401 generic", status == 401 and body["error"]["code"] == "invalid_credentials")

    status, body, _ = call("POST", f"{args.api}/api/v1/auth/login",
                           {"tenant": tenant, "email": email, "password": password})
    check("login 200", status == 200)
    token = body["access_token"]

    status, me, _ = call("GET", f"{args.api}/api/v1/me", token=token,
                         headers={"X-Tenant-Id": "00000000-0000-0000-0000-000000000000"})
    check("identity is server-derived", status == 200 and me["tenant"]["slug"] == tenant
          and me["email"] == email and me["roles"] == ["reader", "report_reader"])

    # M2: a report run streams status events and ends with exactly one terminal event.
    # Without a gateway it fails with model_unavailable; with MODEL_MODE=fake it publishes.
    request = urllib.request.Request(
        f"{args.api}/api/v1/chat",
        data=json.dumps({"question": "Fulfilled sales for Q1 2026?"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        stream = response.read().decode()
    events = [line[7:] for line in stream.splitlines() if line.startswith("event: ")]
    first_content = min((i for i, e in enumerate(events) if e in ("token", "citation")), default=None)
    check("report run ends with one done event", events.count("done") == 1 and events[-1] == "done")
    check("no content before publication",
          first_content is None or "publication" in stream.split("event: token", 1)[0])

    status, _, _ = call("POST", f"{args.api}/api/v1/auth/logout", token=token)
    check("logout 204", status == 204)
    status, body, _ = call("GET", f"{args.api}/api/v1/me", token=token)
    check("revoked session 401", status == 401 and body["error"]["code"] == "unauthenticated")

    inactive = "inactive.reader@niletech.example"
    status, _, _ = call("POST", f"{args.api}/api/v1/auth/login",
                        {"tenant": tenant, "email": inactive, "password": creds[tenant][inactive]})
    check("inactive user 401", status == 401)

    status, _, headers = call("OPTIONS", f"{args.api}/api/v1/auth/login", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    check("CORS preflight for dev frontend",
          headers.get("access-control-allow-origin") == "http://localhost:3000")
    print("smoke: all checks passed")


if __name__ == "__main__":
    main()
