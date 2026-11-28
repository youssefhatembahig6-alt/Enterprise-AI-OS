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
run ``reindex`` once. It re-embeds ONLY the synthetic demo fixtures
(``backend/fixtures/documents/corpus.toml`` for NileTech and
``backend/fixtures/legal/legal.toml``): a document is sent only if its title, department
and classification match a fixture AND its current version holds that fixture file's
exact bytes (same filename and SHA-256), and only if exactly one document matches. The
reindex request carries the expected version and hash, and the backend re-checks them
under a row lock (409 otherwise), so nothing else reaches the gateway. It reports each
fixture's state and whether it is searchable (exit status 1 if an expected one is not).

Transport: an external gateway must use https (plain http only for loopback or a
private network, as the backend enforces), and no request that carries the token ever
follows a redirect.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import subprocess
import sys
import time
import tomllib
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow 3xx and resend the Authorization header to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(
    method: str, url: str, token: str | None = None, body: Any = None, timeout: float = 30
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json", "User-Agent": "eaios-connect"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            sys.exit(
                f"refused: the gateway answered with a redirect ({exc.code}); not following it"
            )
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
    if parts.scheme == "http" and not local_host(parts.hostname):
        sys.exit("--url must use https for a gateway outside this machine or network")
    return root


def local_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


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
    status, ready = http("GET", f"{root}/ready", token, timeout=30)
    if status in (401, 403):
        sys.exit("the gateway rejected the token (Colab restarted with another token?)")
    if status == 503:
        sys.exit("the gateway is up but its Qwen chat server is not ready (cell 3 still loading?)")
    if status != 200 or not ((ready or {}).get("chat") or {}).get("ready"):
        sys.exit(f"authenticated readiness check failed (HTTP {status}; gateway too old?)")
    print(
        f"gateway OK from the laptop: {chat['model']}@{chat['revision'][:7]}, "
        "token accepted, Qwen chat server ready"
    )
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


FIXTURES = ROOT / "backend" / "fixtures"


def fixture_manifest() -> dict[str, dict[str, Any]]:
    """The synthetic demo fixtures as uploaded by the seeds: title -> exact file bytes
    (SHA-256), filename, department, classification, and why one is expected NOT to be
    searchable (None: expected searchable). NileTech corpus documents and the contracts."""

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    corpus = tomllib.loads((FIXTURES / "documents" / "corpus.toml").read_text())
    legal = tomllib.loads((FIXTURES / "legal" / "legal.toml").read_text())
    manifest: dict[str, dict[str, Any]] = {}
    for doc in corpus["documents"]:
        if doc.get("tenant") != "niletech":
            continue
        perms = doc.get("permissions")
        reason = None
        if perms is None:
            reason = "no permissions by design (never searchable)"
        elif doc.get("expect_status") == "needs_ocr":
            reason = "scanned PDF: needs Docling OCR on the gateway"
        manifest[doc["title"]] = {
            "filename": doc["file"],
            "sha256": sha(FIXTURES / "documents" / "files" / doc["file"]),
            "department": (perms or {}).get("department", "finance"),
            # Uploaded without permissions: the backend stores it as unknown (never eligible).
            "permission_state": "verified" if perms else "unknown",
            "classification": perms["classification"] if perms else None,
            "reason": reason,
        }
    for c in legal["contracts"]:
        manifest[c["title"]] = {
            "filename": c["file"],
            "sha256": sha(FIXTURES / "legal" / "files" / c["file"]),
            "department": c["department"],
            "permission_state": "verified",
            "classification": c["permissions"]["classification"],
            "reason": None,
        }
    return manifest


def _mismatch(doc: dict[str, Any], spec: dict[str, Any]) -> str | None:
    """Why ``doc`` is not the fixture ``spec`` as seeded (None: identical identity AND
    current content). Needs the administrator view (``versions`` with byte hashes)."""

    if not doc.get("can_administer"):
        return "not administered by the reindexing account"
    if doc.get("department") != spec["department"]:
        return f"department {doc.get('department')!r} is not the fixture's {spec['department']!r}"
    if doc.get("permission_state") != spec["permission_state"]:
        return f"permission state {doc.get('permission_state')!r} is not the fixture's"
    if spec["classification"] is not None and doc.get("classification") != spec["classification"]:
        return f"classification {doc.get('classification')!r} differs from the fixture"
    current = next(
        (v for v in doc.get("versions") or [] if v.get("version") == doc.get("current_version")),
        None,
    )
    if current is None or not current.get("byte_sha256"):
        return "current version or its hash is not visible (backend older than this branch?)"
    if current.get("filename") != spec["filename"]:
        return f"current version's file {current.get('filename')!r} is not {spec['filename']!r}"
    if current["byte_sha256"] != spec["sha256"]:
        return (
            f"current content (version {doc.get('current_version')}) differs from the fixture file"
        )
    return None


def select_fixtures(
    docs: list[dict[str, Any]], manifest: dict[str, dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[str, str]], list[str]]:
    """Which documents may be sent for embedding: exactly one document per fixture whose
    identity and CURRENT content equal the fixture file. Returns (targets, rejected
    (title, reason), missing fixture titles). Documents with other titles are ignored."""

    targets: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected: list[tuple[str, str]] = []
    missing: list[str] = []
    for title, spec in manifest.items():
        matches = []
        for doc in (d for d in docs if d.get("title") == title):
            reason = _mismatch(doc, spec)
            if reason is None:
                matches.append(doc)
            else:
                rejected.append((f"{title} [{doc['document_id'][:8]}]", reason))
        if len(matches) == 1:
            targets.append((matches[0], spec))
        elif matches:
            for doc in matches:
                rejected.append(
                    (
                        f"{title} [{doc['document_id'][:8]}]",
                        f"ambiguous: {len(matches)} documents match this fixture exactly",
                    )
                )
            missing.append(title)
        else:
            missing.append(title)
    return targets, rejected, missing


# Patched by tests to simulate slow workers without waiting.
_sleep = time.sleep
_now = time.monotonic


def judge(
    spec: dict[str, Any], job: dict[str, Any] | None, doc: dict[str, Any] | None
) -> str | None:
    """Why a queued fixture is NOT in the expected end state (None: it is). Expected
    searchable: its job ended ``active`` in the current embedding space with chunks, and
    the document is searchable. Expected not searchable: the scanned note ends
    ``needs_ocr``; the no-permission memo is indexed but never searchable."""

    if job is None:
        return "job outcome unknown"
    outcome = job.get("outcome")
    if outcome == "pending":
        return f"job still {job.get('status')} (timed out)"
    reason = spec["reason"]
    if reason is not None:
        if doc is not None and doc.get("searchable"):
            return "searchable although it should not be"
        if spec["permission_state"] == "verified" and outcome != "needs_ocr":
            return f"expected needs_ocr, job outcome {outcome}"
        return None
    if outcome != "active":
        error = (job.get("error") or {}).get("code")
        return f"job outcome {outcome}" + (f" ({error})" if error else "")
    if not job.get("in_current_space"):
        return (
            f"active generation is not in the current embedding space {job.get('embedding_space')}"
        )
    if not job.get("chunk_count"):
        return "active generation has no chunks"
    if doc is None or not doc.get("searchable"):
        return "document is not searchable"
    return None


def reindex(timeout: float = 1800, poll: float = 5.0) -> None:  # noqa: PLR0915 - one linear procedure
    """Queue the verified fixtures, wait for THOSE jobs, then confirm each fixture's end
    state. Exit status 1 on any rejected request, failed job, wrong embedding space or
    timeout; a document that only looks searchable (old generation) is not enough."""

    token = admin_token()
    status, docs = http("GET", f"{API}/api/v1/documents", token)
    if status != 200:
        sys.exit(f"could not list documents (HTTP {status})")
    manifest = fixture_manifest()
    targets, rejected, missing = select_fixtures(docs, manifest)
    others = sum(1 for d in docs if d["title"] not in manifest)
    print(
        f"verified {len(targets)} synthetic fixture(s) by identity and current content; "
        f"{len(rejected)} same-title document(s) refused; {others} other document(s) ignored"
    )
    for label, reason in rejected:
        print(f"  NOT sent: {label}: {reason}")
    problems = [f"fixture not reindexed: {t}" for t in sorted(set(missing))]
    jobs: dict[str, dict[str, Any]] = {}  # job id -> {"doc", "spec", "state"}
    for doc, spec in targets:
        try:
            code, body = http(
                "POST",
                f"{API}/api/v1/documents/{doc['document_id']}/reindex",
                token,
                {
                    "expected_version": doc["current_version"],
                    "expected_byte_sha256": spec["sha256"],
                },
            )
        except OSError as exc:
            code, body = 0, {"error": type(exc).__name__}
        job_id = (body or {}).get("job_id") if code == 202 else None
        if not job_id:
            why = "changed after the check" if code == 409 else f"HTTP {code or 'unreachable'}"
            print(f"  REJECTED: {doc['title']}: {why}")
            problems.append(f"reindex request rejected for {doc['title']}: {why}")
            continue
        print(f"  queued {doc['title']} (version {doc['current_version']}): job {job_id[:8]}")
        jobs[job_id] = {"doc": doc, "spec": spec, "state": None}
    deadline = _now() + timeout
    while True:
        pending = 0
        for job_id, entry in jobs.items():
            if entry["state"] and entry["state"].get("outcome") != "pending":
                continue
            url = f"{API}/api/v1/documents/{entry['doc']['document_id']}/jobs/{job_id}"
            try:
                code, state = http("GET", url, token)
            except OSError:
                code, state = 0, None
            if code == 200:
                entry["state"] = state
            if not entry["state"] or entry["state"].get("outcome") == "pending":
                pending += 1
        if not pending:
            break
        if _now() >= deadline:
            problems.append(f"timed out after {timeout:.0f} s with {pending} job(s) unfinished")
            break
        print(f"waiting for {pending} job(s)…", flush=True)
        _sleep(poll)
    try:
        _, docs = http("GET", f"{API}/api/v1/documents", token)
    except OSError:
        docs = []
    by_id = {d["document_id"]: d for d in docs or []}
    print("\nsynthetic fixtures after reindex:")
    searchable = 0
    for _job_id, entry in sorted(jobs.items(), key=lambda kv: kv[1]["doc"]["title"]):
        doc, spec, state = entry["doc"], entry["spec"], entry["state"]
        now_doc = by_id.get(doc["document_id"])
        why = judge(spec, state, now_doc)
        outcome = (state or {}).get("outcome", "unknown")
        space = "current space" if (state or {}).get("in_current_space") else "-"
        note = "" if spec["reason"] is None else f"  (expected not searchable: {spec['reason']})"
        print(
            f"  {'OK ' if why is None else 'BAD'} job {outcome:<10} {space:<13}"
            f" searchable={bool(now_doc and now_doc.get('searchable'))!s:<5} {doc['title']}{note}"
            + (f"  <- {why}" if why else "")
        )
        if why:
            problems.append(f"{doc['title']}: {why}")
        elif spec["reason"] is None:
            searchable += 1
    expected = sum(1 for spec in manifest.values() if spec["reason"] is None)
    print(
        f"\n{searchable} of {expected} expected-searchable synthetic fixtures are searchable "
        "in the active model's embedding space"
    )
    if problems:
        sys.exit("reindex FAILED:\n  " + "\n  ".join(problems))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("connect")
    c.add_argument("--url", required=True, help="gateway root, e.g. https://<host> (no /v1)")
    sub.add_parser("disconnect")
    sub.add_parser("status")
    r = sub.add_parser("reindex")
    r.add_argument("--timeout", type=float, default=1800, help="seconds to wait for the jobs")
    args = ap.parse_args()
    if args.cmd == "connect":
        connect(args.url)
    elif args.cmd == "disconnect":
        disconnect()
    elif args.cmd == "reindex":
        reindex(timeout=args.timeout)
    else:
        print(describe(backend_status()))


if __name__ == "__main__":
    main()
