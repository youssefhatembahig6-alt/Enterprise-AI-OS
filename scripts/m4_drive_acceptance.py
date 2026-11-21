#!/usr/bin/env python3
"""M4 live Google Drive acceptance run against the running stack. Standard library only.

    python3 scripts/m4_drive_acceptance.py --folder <Drive folder id> --reader <Google email>

Prerequisites (docs/evidence/m4/README.md, "Live Drive run"):

1. ``DRIVE_CONNECTOR_CONFIG`` points at an operator file that binds ``niletech`` to real
   Drive credentials and lists ``--folder`` in ``allowed_roots``.
2. The folder holds a text document that answers ``--question`` and is shared (reader)
   with the ``--reader`` Google account.
3. ``--reader`` exists as a NileTech user:
   ``docker compose run --rm -v "$PWD/var:/app/var" migrate python -m app.cli add-drive-reader --email <reader>``

Steps, each timestamped: create or reuse the source, sync, wait for ingestion, ask as the
reader (expect a published answer with a citation), open the citation, then ask the
operator to remove the reader from the file's sharing in Drive, sync again, ask again
(expect ``no_sources``), reopen the citation (expect 404) and replay the first run
(expect ``access_revoked``). Writes ``var/m4-drive-acceptance-<UTC>.json``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_Q = "What is the daily meal allowance for international travel?"


def now() -> str:
    return datetime.now(UTC).isoformat()


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, token: str | None = None, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310 - local API
                raw = response.read().decode()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode(), exc.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def login(self, tenant: str, email: str, password: str) -> str:
        status, body = self.call("POST", "/api/v1/auth/login", body={"tenant": tenant, "email": email, "password": password})
        if status != 200:
            sys.exit(f"login failed for {email}: HTTP {status}")
        return str(body["access_token"])


def sse(raw: str) -> list[dict[str, Any]]:
    frames = []
    for block in raw.split("\n\n"):
        event, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if event and data:
            frames.append({"event": event, "data": json.loads("\n".join(data))})
    return frames


def ask(api: Api, token: str, question: str) -> dict[str, Any]:
    started = time.monotonic()
    status, raw = api.call(
        "POST", "/api/v1/chat", token,
        {"question": question, "mode": "documents", "client_request_id": secrets.token_hex(12)},
    )
    frames = sse(raw) if isinstance(raw, str) else []
    done = next((f["data"] for f in frames if f["event"] == "done"), None)
    return {
        "at": now(),
        "http_status": status,
        "seconds": round(time.monotonic() - started, 2),
        "run_id": done.get("run_id") if done else None,
        "events": [f["event"] for f in frames],
        "outcome": done.get("outcome") if done else None,
        "abstention": done.get("abstention") if done else None,
        "inference": done.get("inference") if done else None,
        "citations": [f["data"] for f in frames if f["event"] == "citation"],
        "answer": [f["data"].get("text") for f in frames if f["event"] == "token"],
        "errors": [f["data"] for f in frames if f["event"] == "error"],
    }


def wait_sync(api: Api, token: str, source_id: str, timeout: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, source = api.call("GET", f"/api/v1/admin/sources/{source_id}", token)
        if source.get("last_sync_status") not in (None, "running"):
            return dict(source)
        time.sleep(2)
    sys.exit("sync did not finish in time")


def sync(api: Api, token: str, source_id: str) -> dict[str, Any]:
    requested = now()
    status, body = api.call("POST", f"/api/v1/admin/sources/{source_id}/sync", token)
    if status != 202:
        sys.exit(f"sync request failed: HTTP {status} {body}")
    if body.get("status") == "queued":
        time.sleep(1)
    source = wait_sync(api, token, source_id)
    run = (source.get("recent_runs") or [{}])[0]
    return {
        "requested_at": requested,
        "response": body,
        "run": run,
        "access": source["access"],
        "last_verified_sync_at": source["last_verified_sync_at"],
        "items": source.get("items", []),
    }


def main() -> None:  # noqa: PLR0915 - one linear acceptance script
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--tenant", default="niletech")
    parser.add_argument("--folder", required=True)
    parser.add_argument("--reader", required=True, help="Google account the file is shared with")
    parser.add_argument("--admin", default="access.admin@niletech.example")
    parser.add_argument("--question", default=DEFAULT_Q)
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    parser.add_argument("--no-prompt", action="store_true", help="skip the revoke step")
    args = parser.parse_args()

    creds = json.loads(Path(args.credentials).read_text())[args.tenant]
    api = Api(args.api)
    record: dict[str, Any] = {"started_at": now(), "folder": args.folder, "question": args.question}
    admin = api.login(args.tenant, args.admin, creds[args.admin])
    reader = api.login(args.tenant, args.reader.lower(), creds[args.reader.lower()])

    _, settings = api.call("GET", "/api/v1/tenant/settings", admin)
    record["tenant_settings"] = settings
    _, sources = api.call("GET", "/api/v1/admin/sources", admin)
    source = next((s for s in sources if s["external_root"] == args.folder), None)
    if source is None:
        status, source = api.call("POST", "/api/v1/admin/sources", admin, {
            "name": "M4 live Drive test folder", "external_root": args.folder,
            "department": "finance", "classification": "internal", "allowed_countries": ["*"],
        })
        if status != 201:
            sys.exit(f"source creation failed: HTTP {status} {source}")
    source_id = source["source_id"]
    record["source_id"] = source_id

    record["sync_1"] = first = sync(api, admin, source_id)
    print(f"sync 1: {first['run'].get('status')} verified at {first['last_verified_sync_at']}")
    if first["access"] != "current":
        record["result"] = "FAIL: first sync did not verify permissions"
        return finish(record)
    docs = [i["document_id"] for i in first["items"] if i["document_id"]]
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        _, listed = api.call("GET", "/api/v1/admin/documents", admin)
        states = {d["document_id"]: d["status"] for d in listed if d["document_id"] in docs}
        if states and all(s in ("ready", "failed", "needs_ocr") for s in states.values()):
            break
        time.sleep(3)
    record["ingestion"] = {"at": now(), "states": states}

    record["answer_before"] = before = ask(api, reader, args.question)
    print(f"answer before revoke: {before['outcome']} in {before['seconds']} s ({before['inference']})")
    citation = before["citations"][0] if before["citations"] else None
    if citation:
        status, opened = api.call("GET", f"/api/v1/runs/{before['run_id']}/citations/{citation['citation_id']}", reader)
        record["citation_before"] = {"at": now(), "http_status": status, "body": opened}

    if args.no_prompt:
        record["result"] = "PARTIAL: revoke step skipped (--no-prompt)"
        return finish(record)
    input(f"\nRemove {args.reader} from the file's sharing in Drive now, then press Enter… ")
    record["revoked_in_drive_at"] = now()
    record["sync_2"] = second = sync(api, admin, source_id)
    print(f"sync 2: {second['run'].get('status')} stats {second['run'].get('stats')}")
    record["answer_after"] = after = ask(api, reader, args.question)
    print(f"answer after revoke: {after['outcome']} {after['abstention']}")
    if citation:
        status, _ = api.call("GET", f"/api/v1/runs/{before['run_id']}/citations/{citation['citation_id']}", reader)
        record["citation_after"] = {"at": now(), "http_status": status}
        status, raw = api.call("GET", f"/api/v1/runs/{before['run_id']}/events?after=0", reader)
        frames = sse(raw) if isinstance(raw, str) else []
        record["replay_after"] = {
            "at": now(),
            "http_status": status,
            "events": [f["event"] for f in frames],
            "errors": [f["data"] for f in frames if f["event"] == "error"],
        }
    checks = {
        "first_sync_current": first["access"] == "current",
        "authorized_answer_published": before["outcome"] == "published" and bool(citation),
        "citation_opened": record.get("citation_before", {}).get("http_status") == 200,
        "second_sync_succeeded": second["run"].get("status") == "succeeded",
        "sharing_change_detected": (second["run"].get("stats") or {}).get("permissions_changed", 0) >= 1,
        "denied_after_revoke": (after["abstention"] or {}).get("code") == "no_sources",
        "citation_denied_after_revoke": record.get("citation_after", {}).get("http_status") == 404,
        "replay_revoked": any(e.get("code") == "access_revoked" for e in record.get("replay_after", {}).get("errors", [])),
    }
    record["checks"] = checks
    record["result"] = "PASS" if all(checks.values()) else "FAIL"
    finish(record)


def finish(record: dict[str, Any]) -> None:
    record["finished_at"] = now()
    out = ROOT / "var" / f"m4-drive-acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True))
    print(f"\n{record['result']}\nwrote {out}")


if __name__ == "__main__":
    main()
