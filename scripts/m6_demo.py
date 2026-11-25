#!/usr/bin/env python3
"""M6 demonstration against the running stack: verified report -> email draft ->
human approval -> durable outbox -> Mailpit. Standard library only.

    python3 scripts/m6_demo.py [--api http://localhost:8000] [--mailpit http://localhost:8025]

Steps (all through the public API, as the fixture users in var/dev-credentials.json):

1. niletech/access-admin enables the ``actions`` pack and ``mailpit`` connector (FR-12).
2. niletech/finance-reader asks the frozen Q1 2026 sales question and waits for the
   verified report (real or fake inference is read from the server's labels).
3. A draft to an unauthorized recipient (board member) is refused.
4. A draft to the finance reader's own address is created, edited (version 2), and the
   stale version-1 approval is shown to be rejected.
5. Version 2 is approved and sent; a forged token and a reused token are rejected.
6. The script waits for ``delivered`` and fetches the message from Mailpit's API by its
   Message-ID, then writes var/m6-demo-<UTC timestamp>.json with every response.
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
QUESTION = "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?"
TENANT = "niletech"
SENDER = "finance.reader@niletech.example"
ADMIN = "access.admin@niletech.example"
UNAUTHORIZED = "board.member@niletech.example"


class Http(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"{status}: {body}")
        self.status, self.body = status, body


def call(method: str, url: str, token: str | None = None, body: object = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - local
            raw = response.read()
            return json.loads(raw) if raw and raw[:1] in b"[{" else raw.decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise Http(exc.code, json.loads(raw) if raw else None) from None


def login(api: str, creds: dict[str, Any], email: str) -> str:
    body = {"tenant": TENANT, "email": email, "password": creds[TENANT][email]}
    return str(call("POST", f"{api}/api/v1/auth/login", body=body)["access_token"])


def sse_events(raw: str) -> list[dict[str, Any]]:
    events = []
    for block in raw.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in block.split("\n") if ": " in line and line[0] != ":"
        )
        if "event" in fields:
            events.append({"event": fields["event"], "data": json.loads(fields["data"])})
    return events


def expect_error(log: dict[str, Any], key: str, fn: Any, status: int) -> None:
    try:
        fn()
    except Http as exc:
        log[key] = {"status": exc.status, "body": exc.body}
        if exc.status != status:
            raise SystemExit(f"{key}: expected {status}, got {exc.status}") from None
        print(f"  ok  {key}: {exc.status} {exc.body['error']['code']}")
        return
    raise SystemExit(f"{key}: expected HTTP {status}, the call succeeded")


def main() -> None:  # noqa: PLR0915 - a linear script
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--mailpit", default="http://localhost:8025")
    parser.add_argument("--credentials", default=str(ROOT / "var/dev-credentials.json"))
    args = parser.parse_args()
    api, actions = args.api.rstrip("/"), args.api.rstrip("/") + "/api/v1/actions"
    creds = json.loads(Path(args.credentials).read_text())
    log: dict[str, Any] = {"started_at": datetime.now(UTC).isoformat(), "api": api}

    admin = login(api, creds, ADMIN)
    settings = call("GET", f"{api}/api/v1/tenant/settings", admin)
    packs = sorted({*settings["enabled_packs"], "actions"})
    connectors = sorted({*settings["enabled_connectors"], "mailpit"})
    log["tenant_settings"] = call(
        "PUT",
        f"{api}/api/v1/admin/tenant/settings",
        admin,
        {"enabled_packs": packs, "enabled_connectors": connectors},
    )
    print("1. actions pack and mailpit connector enabled by the access admin")

    token = login(api, creds, SENDER)
    started = time.monotonic()
    raw = call("POST", f"{api}/api/v1/chat", token, {"question": QUESTION, "mode": "report"})
    events = sse_events(raw)
    done = next(e["data"] for e in events if e["event"] == "done")
    log["report"] = {"seconds": round(time.monotonic() - started, 2), "events": events}
    if done["outcome"] != "published":
        raise SystemExit(f"report not published: {done}")
    run_id = done["run_id"]
    verification = done["verification"]
    print(f"2. verified report published: run {run_id}, {verification['label']},"
          f" inference={verification['inference_mode']}")

    recipients = {r["email"]: r for r in call("GET", f"{actions}/recipients", token)}
    expect_error(
        log,
        "draft_to_unauthorized_recipient",
        lambda: call("POST", f"{actions}/email-drafts", token,
                     {"source_run_id": run_id,
                      "recipient_user_id": recipients[UNAUTHORIZED]["user_id"]}),
        422,
    )
    print("3. a recipient without dataset access cannot be drafted")

    me = recipients[SENDER]["user_id"]
    v1 = call("POST", f"{actions}/email-drafts", token,
              {"source_run_id": run_id, "recipient_user_id": me})
    log["draft_v1"] = v1
    approval_v1 = call("POST", f"{actions}/email-drafts/{v1['action_id']}/approve", token,
                       {"version": 1, "draft_sha256": v1["draft_sha256"]})
    v2 = call("PUT", f"{actions}/email-drafts/{v1['action_id']}", token, {
        "version": 1, "draft_sha256": v1["draft_sha256"], "recipient_user_id": me,
        "subject": v1["subject"] + " (reviewed)",
        "body": v1["body"] + "\n\nReviewed before sending.",
    })
    log["draft_v2"] = v2
    print(f"4. draft {v1['action_id']} v1 approved, then edited to v2")
    expect_error(
        log,
        "send_with_pre_edit_approval",
        lambda: call("POST", f"{actions}/email-drafts/{v1['action_id']}/send", token, {
            "version": 2, "draft_sha256": v2["draft_sha256"],
            "approval_token": approval_v1["approval_token"]}),
        409,
    )
    expect_error(
        log,
        "send_with_forged_approval",
        lambda: call("POST", f"{actions}/email-drafts/{v1['action_id']}/send", token, {
            "version": 2, "draft_sha256": v2["draft_sha256"],
            "approval_token": secrets.token_urlsafe(32)}),
        409,
    )
    approval = call("POST", f"{actions}/email-drafts/{v1['action_id']}/approve", token,
                    {"version": 2, "draft_sha256": v2["draft_sha256"]})
    log["approval_v2"] = {k: v for k, v in approval.items() if k != "approval_token"}
    sent = call("POST", f"{actions}/email-drafts/{v1['action_id']}/send", token, {
        "version": 2, "draft_sha256": v2["draft_sha256"],
        "approval_token": approval["approval_token"]})
    log["send"] = sent
    print(f"5. v2 approved (expires {approval['expires_at']}) and queued: {sent['status']}")
    expect_error(
        log,
        "send_reusing_approval",
        lambda: call("POST", f"{actions}/email-drafts/{v1['action_id']}/send", token, {
            "version": 2, "draft_sha256": v2["draft_sha256"],
            "approval_token": approval["approval_token"]}),
        409,
    )

    deadline = time.monotonic() + 120
    final = sent
    while final["status"] not in ("delivered", "blocked", "failed", "cancelled"):
        if time.monotonic() > deadline:
            raise SystemExit(f"not delivered in time: {final['status']}")
        time.sleep(1)
        final = call("GET", f"{actions}/email-drafts/{v1['action_id']}", token)
    log["final"] = final
    if final["status"] != "delivered":
        raise SystemExit(f"delivery ended as {final['status']}: {final['status_reason']}")
    message_id = final["delivery"]["message_id"].strip("<>")
    query = urllib.parse.urlencode({"query": f"message-id:{message_id}"})
    found = call("GET", f"{args.mailpit}/api/v1/search?{query}")
    matches = [m for m in found["messages"] if m["MessageID"] == message_id]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one Mailpit message, found {len(matches)}")
    message = call("GET", f"{args.mailpit}/api/v1/message/{matches[0]['ID']}")
    log["mailpit"] = {
        "id": message["ID"],
        "message_id": message["MessageID"],
        "to": [a["Address"] for a in message["To"]],
        "subject": message["Subject"],
        "text": message["Text"],
    }
    print(f"6. delivered: exactly one Mailpit message {message['ID']} with Message-ID"
          f" <{message_id}> to {log['mailpit']['to']}")
    log["finished_at"] = datetime.now(UTC).isoformat()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "var" / f"m6-demo-{stamp}.json"
    out.write_text(json.dumps(log, indent=2, ensure_ascii=False))
    print(f"evidence: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except Http as exc:
        sys.exit(f"HTTP {exc.status}: {exc.body}")
