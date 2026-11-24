#!/usr/bin/env python3
"""M5 live GitHub acceptance run against the running stack. Standard library only.

    python3 scripts/m5_github_acceptance.py --repo <owner/name> --login <github login> \
        --path <file in the repo> --question "<code question answered by that file>"

Prerequisites (docs/evidence/m5/README.md, "Live GitHub run"):

1. A **dedicated test repository** (private) and a fine-grained token with read access
   to its contents and metadata and permission to list collaborators. The token lives
   only in the operator file: ``GITHUB_CONNECTOR_CONFIG`` binds ``niletech`` to it and
   lists ``--repo`` in ``allowed_repositories``.
2. ``--login`` is a collaborator with read access to the repository (a second GitHub
   account, so removing it does not lock out the repository owner).
3. The stack runs this branch (migration 0011) and the NileTech seed enables the
   ``github`` connector and the ``code`` pack.

Checks, in order (each sync request waits for the run it created):

1. the tenant admin links ``--login`` to ``--reader`` (default finance.reader);
2. sync 1 succeeds; the source is ``current``; ``--path`` is an active item;
3. the reader's code answer is published and its citation names the repository,
   ``--path``, the synced commit and line numbers; the citation reopens (200);
4. (the operator removes ``--login`` from the repository on GitHub)
5. sync 2 succeeds with ``collaborators_removed >= 1``;
6. the same question is refused (``no_sources``) and the citation no longer opens (404).

Evidence with a timestamp per step is always written to
``var/m5-github-acceptance-<UTC>.json``. Exit status: 0 PASS, 1 a check failed,
2 timeout, 3 setup error. ``--no-revocation`` stops after step 3 (recorded as such).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = frozenset({"succeeded", "failed", "skipped", "superseded"})


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
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - local stack
                raw = response.read().decode()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode(), exc.code
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw

    def ask(self, token: str, question: str) -> dict[str, Any]:
        body = json.dumps({"question": question, "mode": "code"}).encode()
        request = urllib.request.Request(
            f"{self.base}/api/v1/chat",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        events: list[dict[str, Any]] = []
        with urllib.request.urlopen(request, timeout=600) as response:  # noqa: S310
            run_id = response.headers.get("X-Run-ID")
            kind = None
            for line in response.read().decode().splitlines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: ") and kind:
                    events.append({"event": kind, "data": json.loads(line[6:])})
        done = next((e["data"] for e in events if e["event"] == "done"), {})
        return {
            "run_id": run_id,
            "outcome": done.get("outcome"),
            "abstention": (done.get("abstention") or {}).get("code"),
            "citations": [e["data"] for e in events if e["event"] == "citation"],
        }


def credentials(email: str) -> str:
    data = json.loads((ROOT / "var" / "dev-credentials.json").read_text())
    return str(data["niletech"][email])


def main() -> int:  # noqa: PLR0915 - one linear acceptance script
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--login", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--reader", default="finance.reader@niletech.example")
    parser.add_argument("--admin", default="access.admin@niletech.example")
    parser.add_argument("--department", default="it")
    parser.add_argument("--no-revocation", action="store_true")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    api = Api(args.api)
    evidence: dict[str, Any] = {
        "kind": "m5_github_acceptance",
        "live": True,
        "repository": args.repo,
        "path": args.path,
        "started_at": now(),
        "steps": [],
    }
    out = ROOT / "var" / f"m5-github-acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"

    def step(name: str, ok: bool, **detail: Any) -> bool:
        evidence["steps"].append({"step": name, "ok": ok, "at": now(), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        return ok

    def finish(code: int) -> int:
        evidence["finished_at"] = now()
        evidence["result"] = {0: "pass", 1: "fail", 2: "timeout", 3: "setup_error"}[code]
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2))
        print(f"evidence: {out}")
        return code

    def login(email: str) -> str:
        status, body = api.call(
            "POST", "/api/v1/auth/login", body={"tenant": "niletech", "email": email, "password": credentials(email)}
        )
        if status != 200:
            raise SystemExit(finish(3))
        return str(body["access_token"])

    def sync(admin: str, source_id: str) -> dict[str, Any]:
        status, started = api.call("POST", f"/api/v1/admin/sources/{source_id}/sync", admin)
        if status != 202:
            return {"status": "request_failed", "http": status, "body": started}
        run_id = started["run_id"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            _, run = api.call("GET", f"/api/v1/admin/sources/{source_id}/runs/{run_id}", admin)
            if run and run.get("status") in TERMINAL:
                return dict(run)
            time.sleep(2)
        raise SystemExit(finish(2))

    try:
        admin = login(args.admin)
        reader = login(args.reader)
    except (OSError, KeyError) as exc:
        evidence["setup_error"] = type(exc).__name__
        return finish(3)
    _, users = api.call("GET", "/api/v1/admin/users", admin)
    reader_id = next((u["user_id"] for u in users or [] if u["email"] == args.reader), None)
    status, _ = api.call("PUT", f"/api/v1/admin/users/{reader_id}/external-accounts", admin, {"github": args.login})
    if not step("link_login_to_reader", status == 200, http=status):
        return finish(3)
    _, sources = api.call("GET", "/api/v1/admin/sources", admin)
    source = next((s for s in sources or [] if s["kind"] == "github" and s["external_root"].lower() == args.repo.lower()), None)
    if source is None:
        status, source = api.call(
            "POST",
            "/api/v1/admin/sources",
            admin,
            {
                "kind": "github",
                "name": args.repo,
                "external_root": args.repo,
                "department": args.department,
                "classification": "internal",
                "allowed_countries": ["*"],
            },
        )
        if not step("create_source", status == 201, http=status, error=(source or {}).get("error")):
            return finish(3)
    sid = source["source_id"]
    run = sync(admin, sid)
    _, view = api.call("GET", f"/api/v1/admin/sources/{sid}", admin)
    item = next((i for i in view.get("items", []) if i["path"] == args.path), None)
    ok = run.get("status") == "succeeded" and view.get("access") == "current" and bool(item)
    step("sync_1", ok, run=run, access=view.get("access"), commit=view.get("external_revision"), item=item)
    if not ok:
        return finish(1)
    doc = item["document_id"]
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        _, detail = api.call("GET", f"/api/v1/documents/{doc}", admin)
        if (detail or {}).get("status") in ("ready", "failed"):
            break
        time.sleep(2)
    if not step("ingested", (detail or {}).get("status") == "ready", status=(detail or {}).get("status")):
        return finish(1)
    answer = api.ask(reader, args.question)
    cited = [c for c in answer["citations"] if c.get("path") == args.path]
    ok = (
        answer["outcome"] == "published"
        and bool(cited)
        and cited[0].get("repository", "").lower() == args.repo.lower()
        and cited[0].get("commit") == view.get("external_revision")
        and isinstance(cited[0].get("line_start"), int)
    )
    step("code_answer_cites_repository_file_revision_lines", ok, answer=answer)
    if not ok:
        return finish(1)
    cid = cited[0]["citation_id"]
    status, opened = api.call("GET", f"/api/v1/runs/{answer['run_id']}/citations/{cid}", reader)
    if not step("citation_opens", status == 200 and opened.get("integrity") == "verified", http=status):
        return finish(1)
    if args.no_revocation:
        evidence["revocation"] = "not run (--no-revocation)"
        return finish(0)
    input(f"Remove '{args.login}' from {args.repo} on GitHub (Settings > Collaborators), then press Enter: ")
    run = sync(admin, sid)
    removed = (run.get("stats") or {}).get("collaborators_removed", 0)
    if not step("sync_2_detects_removal", run.get("status") == "succeeded" and removed >= 1, run=run):
        return finish(1)
    again = api.ask(reader, args.question)
    status, _ = api.call("GET", f"/api/v1/runs/{answer['run_id']}/citations/{cid}", reader)
    ok = again["abstention"] == "no_sources" and status == 404
    step("revoked_answer_and_citation", ok, answer=again, citation_http=status)
    return finish(0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
