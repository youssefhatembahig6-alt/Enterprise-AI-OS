#!/usr/bin/env python3
"""Capture an M2 report run end to end against the running stack. Standard library only.

    python3 scripts/m2_demo_capture.py [--question "..."] [--user finance.reader@niletech.example]

Signs in with the development credentials file, asks the question through
POST /api/v1/chat, records every raw SSE frame with arrival time, then fetches the run
(stage timings, served model revisions) and the evidence record. Writes
var/m2-demo-<UTC timestamp>.json and prints a short summary. Whether the run used real
or fake inference is taken from the server's own labels, never assumed.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_Q = "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?"


def request(method: str, url: str, token: str | None = None, body: object = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.urlopen(  # noqa: S310 - local API URL from the operator
        urllib.request.Request(url, data=data, headers=headers, method=method), timeout=900
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--tenant", default="niletech")
    parser.add_argument("--user", default="finance.reader@niletech.example")
    parser.add_argument("--question", default=DEFAULT_Q)
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    args = parser.parse_args()

    password = json.loads(Path(args.credentials).read_text())[args.tenant][args.user]
    with request("POST", f"{args.api}/api/v1/auth/login", body={
        "tenant": args.tenant, "email": args.user, "password": password,
    }) as response:
        token = json.load(response)["access_token"]

    frames, started = [], time.monotonic()
    try:
        response = request("POST", f"{args.api}/api/v1/chat", token, {
            "question": args.question, "client_request_id": secrets.token_hex(12),
        })
    except urllib.error.HTTPError as exc:
        sys.exit(f"chat failed: HTTP {exc.code} {exc.read().decode()[:300]}")
    run_id = response.headers.get("X-Run-ID")
    block: list[str] = []
    with response:
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line:
                block.append(line)
                continue
            fields = dict(item.split(": ", 1) for item in block if ": " in item and not item.startswith(":"))
            block = []
            if "event" in fields:
                frames.append({"t": round(time.monotonic() - started, 3), "id": fields.get("id"),
                               "event": fields["event"], "data": json.loads(fields["data"])})
                if fields["event"] == "done":
                    break

    with request("GET", f"{args.api}/api/v1/runs/{run_id}", token) as response:
        run = json.load(response)
    evidence = None
    result = run.get("result") or {}
    if result.get("evidence"):
        with request("GET", f"{args.api}/api/v1/evidence/{result['evidence']['evidence_id']}", token) as r:
            evidence = json.load(r)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "var" / f"m2-demo-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "captured_at": stamp, "api": args.api, "question": args.question, "run_id": run_id,
        "frames": frames, "run": run, "evidence": evidence,
    }, indent=2) + "\n")
    done = frames[-1]["data"] if frames and frames[-1]["event"] == "done" else {}
    print(f"run {run_id}: outcome={done.get('outcome')} inference={done.get('inference')}")
    print(f"first status at {frames[0]['t'] if frames else '-'} s; done at {frames[-1]['t'] if frames else '-'} s")
    if result.get("table"):
        for cell in result["table"]["rows"][0]:
            print(f"  {cell['column']}: {cell['display']}")
    print(f"verification: {(done.get('verification') or {}).get('label')}")
    print(f"wrote {out}")
    if done.get("inference") == "fake":
        print("NOTE: this run used FAKE inference; it is not real-model evidence.")


if __name__ == "__main__":
    main()
