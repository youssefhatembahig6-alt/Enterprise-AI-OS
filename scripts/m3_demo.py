#!/usr/bin/env python3
"""M3 demonstration helper for the running stack. Standard library only.

    python3 scripts/m3_demo.py seed      upload the fixture corpus through the API and wait
    python3 scripts/m3_demo.py capture   authorized answer + citation, and the no-sources
                                         response for a user without access

``seed`` signs in as each tenant's document administrator (development credentials in
var/dev-credentials.json) and uploads backend/fixtures/documents per corpus.toml, with
its permissions. ``capture`` asks one question as niletech/finance-reader and as
niletech/sales-reader, records every raw SSE frame with arrival times, opens the
citation, and writes var/m3-demo-<UTC timestamp>.json. Real or fake inference is taken
from the server's labels, never assumed.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "backend" / "fixtures" / "documents"
QUESTION = "How many invoices were overdue at the reference date?"
USERS = {
    "niletech": {"admin": "access.admin@niletech.example"},
    "isolation-test": {"admin": "finance.admin@isolation.example"},
}


def call(method: str, url: str, token: str | None = None, body: object = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - local API
        raw = response.read()
        return json.loads(raw) if raw else None


def login(api: str, creds: dict[str, Any], tenant: str, email: str) -> str:
    body = {"tenant": tenant, "email": email, "password": creds[tenant][email]}
    return str(call("POST", f"{api}/api/v1/auth/login", body=body)["access_token"])


def seed(api: str, creds: dict[str, Any], timeout: float = 300.0) -> None:
    corpus = tomllib.loads((FIXTURES / "corpus.toml").read_text())
    tokens = {t: login(api, creds, t, u["admin"]) for t, u in USERS.items()}
    emails = {
        "denied-reader": "denied.reader@niletech.example",
        "board-member": "board.member@niletech.example",
    }
    existing = {
        d["title"] for t in tokens.values() for d in call("GET", f"{api}/api/v1/documents", t)
    }
    for doc in corpus["documents"]:
        if doc["title"] in existing:
            print(f"skip   {doc['key']} (already uploaded)")
            continue
        perms = doc.get("permissions")
        body: dict[str, Any] = {
            "filename": doc["file"],
            "title": doc["title"],
            "content_base64": base64.b64encode((FIXTURES / "files" / doc["file"]).read_bytes()).decode(),
            "department": (perms or {}).get("department", "finance"),
        }
        if perms:
            body["permissions"] = {
                "classification": perms["classification"],
                "department_restricted": perms["department_restricted"],
                "allowed_countries": perms["allowed_countries"],
                "grants": [
                    {"effect": g["effect"], "group": g["group"]} if "group" in g
                    else {"effect": g["effect"], "user_email": emails[g["user"]]}
                    for g in perms["grants"]
                ],
            }
        result = call("POST", f"{api}/api/v1/documents", tokens[doc["tenant"]], body)
        print(f"upload {doc['key']} -> {result['document_id']}")
    deadline = time.monotonic() + timeout  # Docling on the gateway can take minutes per PDF
    while time.monotonic() < deadline:
        docs = [d for t in tokens.values() for d in call("GET", f"{api}/api/v1/documents", t)]
        pending = [d["title"] for d in docs if d["status"] in ("queued", "parsing", "indexing")]
        if not pending:
            for d in docs:
                print(f"  {d['status']:<10} searchable={d['searchable']!s:<5} {d['title']}")
            return
        time.sleep(2)
    sys.exit(f"still ingesting: {pending}")


def ask(api: str, token: str, question: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api}/api/v1/chat",
        data=json.dumps({"question": question, "mode": "documents"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    frames, started, block = [], time.monotonic(), []
    with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310
        run_id = response.headers.get("X-Run-ID")
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line:
                block.append(line)
                continue
            fields = dict(i.split(": ", 1) for i in block if ": " in i and not i.startswith(":"))
            block = []
            if "event" in fields:
                frames.append({"t": round(time.monotonic() - started, 3), "id": fields.get("id"),
                               "event": fields["event"], "data": json.loads(fields["data"])})
                if fields["event"] == "done":
                    break
    return {"run_id": run_id, "frames": frames,
            "run": call("GET", f"{api}/api/v1/runs/{run_id}", token)}


def capture(api: str, creds: dict[str, Any], question: str) -> None:
    allowed = login(api, creds, "niletech", "finance.reader@niletech.example")
    denied = login(api, creds, "niletech", "sales.reader@niletech.example")
    authorized = ask(api, allowed, question)
    citations = []
    for frame in authorized["frames"]:
        if frame["event"] == "citation":
            cid = frame["data"]["citation_id"]
            citations.append(call("GET", f"{api}/api/v1/runs/{authorized['run_id']}/citations/{cid}", allowed))
    refused = ask(api, denied, question)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "var" / f"m3-demo-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "captured_at": stamp, "api": api, "question": question,
        "authorized": {"user": "niletech/finance-reader", **authorized, "citations": citations},
        "no_access": {"user": "niletech/sales-reader", **refused},
    }, indent=2, ensure_ascii=False) + "\n")
    done = authorized["frames"][-1]["data"]
    print(f"authorized: outcome={done['outcome']} inference={done['inference']}")
    for c in citations:
        print(f"  [{c['citation_id']}] {c['title']} v{c['version']} chars {c['char_start']}-{c['char_end']}"
              f" integrity={c['integrity']}: {c['quote']!r}")
    refused_done = refused["frames"][-1]["data"]
    print(f"no access: outcome={refused_done['outcome']} code={(refused_done.get('abstention') or {}).get('code')}"
          f" events={[f['event'] for f in refused['frames']]}")
    print(f"wrote {out}")
    if done.get("inference") == "fake":
        print("NOTE: FAKE inference; this is not real-model evidence.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "capture"])
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--timeout", type=float, default=300.0, help="seed: ingestion wait (s)")
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    args = parser.parse_args()
    creds = json.loads(Path(args.credentials).read_text())
    try:
        if args.command == "seed":
            seed(args.api, creds, args.timeout)
        else:
            capture(args.api, creds, args.question)
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code}: {exc.read().decode()[:400]}")


if __name__ == "__main__":
    main()
