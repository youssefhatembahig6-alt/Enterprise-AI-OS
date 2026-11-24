#!/usr/bin/env python3
"""M5 live GitHub acceptance run against the running stack. Standard library only.

    python3 scripts/m5_github_acceptance.py --repo <owner>/<test-repo> --login <second-account> \
        [--mutate gh] [--require-gateway]

Setup (one time): docs/evidence/m5/OPEN-ITEMS-SETUP.md, section 1. The defaults for
``--path``, ``--rename-to``, ``--delete-path`` and the questions match the fixture files
in ``backend/fixtures/github-acceptance/``, which must be committed to the test
repository's default branch before the run.

Two independent channels are used. The application API (as the tenant admin and a
reader) is what is being tested. The operator's own ``gh`` login reads the repository
directly to check every code citation against the file at the cited commit, and (with
``--mutate gh``) makes the repository changes; ``--mutate manual`` asks the operator to
make each change and press Enter instead. The connector token is never read here.

Stages (each sync request waits for the run it created; every check is recorded):

A. Link ``--login`` to ``--reader``; create (or reuse) the repository source; sync; both
   fixture files ingested.
B. Code answers for both files are published; each citation is verified independently:
   repository, path, commit (= the synced head), blob SHA (git id of the bytes read with
   ``gh``), line_start/line_end inside the file, and the quote occurring at exactly
   those lines. The citation also reopens through the API with integrity ``verified``.
C. Rename ``--path`` to ``--rename-to`` and delete ``--delete-path`` on GitHub; sync.
   The reader is **still a collaborator**. Old citations of both files return 404; new
   answers never cite the removed documents or quote the deleted file; the renamed file
   is answered and cited at the new commit, verified independently.
D. Delete the source in the admin API while the reader is still a collaborator (checked
   on GitHub): the source's content stops contributing and its citations return 404.
   The same repository is added again as a new source and the reader is answered again,
   which shows that the deletion, not lost access, removed the content.
E. Remove ``--login`` as a collaborator on GitHub; sync: ``collaborators_removed >= 1``,
   the reader is refused (``no_sources``) and the stage D citation returns 404.

Evidence is always written to ``var/m5-github-acceptance-<UTC>.json`` (no credentials,
no tokens). Exit status: 0 PASS, 1 a check failed, 2 timeout, 3 setup error.
``--stop-after B|C|D`` ends early (the evidence says so).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = frozenset({"succeeded", "failed", "skipped", "superseded"})
DEFAULTS = {
    "path": "inventory/reservations.py",
    "question": "How many minutes do inventory reservations last before they expire?",
    "rename_to": "inventory/holds.py",
    "delete_path": "billing/refunds.py",
    "delete_question": "What is the refund processing fee in percent?",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


class Timeout(Exception):
    pass


class SetupError(Exception):
    pass


# -- independent citation check ------------------------------------------------------------


def git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(raw) + raw).hexdigest()  # noqa: S324 - git object id


def canonical_lines_text(raw: bytes) -> str:
    """The file as the code parser canonicalises it: UTF-8 (BOM dropped), ``\\r\\n`` and
    ``\\r`` to ``\\n``, NFC. Line *n* of this text is line *n* of the file."""

    text = raw[3:].decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8")
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def verify_code_citation(
    citation: dict[str, Any], *, repo: str, path: str, commit: str, raw: bytes
) -> list[str]:
    """Problems found when checking a code citation against the file bytes read directly
    from GitHub at ``commit``. An empty list means it is exact."""

    problems: list[str] = []
    if str(citation.get("repository", "")).lower() != repo.lower():
        problems.append(f"repository {citation.get('repository')!r} != {repo!r}")
    if citation.get("path") != path:
        problems.append(f"path {citation.get('path')!r} != {path!r}")
    if citation.get("commit") != commit:
        problems.append(f"commit {citation.get('commit')!r} != {commit!r}")
    if citation.get("blob_sha") != git_blob_sha(raw):
        problems.append("blob_sha is not the git id of the file at that commit")
    text = canonical_lines_text(raw)
    total = text.count("\n") + 1
    first, last, quote = citation.get("line_start"), citation.get("line_end"), citation.get("quote")
    if not (isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= total):
        problems.append(f"lines {first}-{last} outside 1-{total}")
        return problems
    if not isinstance(quote, str) or not quote:
        problems.append("empty quote")
        return problems
    lines = text.split("\n")
    window = "\n".join(lines[first - 1 : last])
    if quote not in window:
        problems.append(f"quote is not inside lines {first}-{last}")
    exact = False
    start = text.find(quote)
    while start >= 0:
        end = start + len(quote) - 1
        if text.count("\n", 0, start) + 1 == first and text.count("\n", 0, end) + 1 == last:
            exact = True
            break
        start = text.find(quote, start + 1)
    if not exact:
        problems.append(f"quote does not start on line {first} and end on line {last}")
    expected_url = (
        f"https://github.com/{citation.get('repository')}/blob/{commit}/"
        f"{urllib.parse.quote(path)}#L{first}-L{last}"
    )
    if citation.get("url") != expected_url:
        problems.append("permalink does not match the cited commit, path and lines")
    return problems


# -- the two channels --------------------------------------------------------------------------


class RepoAdapter(Protocol):
    """Direct access to the test repository, independent of the application."""

    def read_file(self, repo: str, path: str, ref: str) -> bytes: ...

    def rename(self, repo: str, path: str, new_path: str) -> None: ...

    def delete(self, repo: str, path: str) -> None: ...

    def is_collaborator(self, repo: str, login: str) -> bool: ...

    def remove_collaborator(self, repo: str, login: str) -> None: ...


class GhRepo:
    """The operator's ``gh`` CLI (already signed in, with write access to the test
    repository). Read-only unless ``mutate`` is True."""

    def __init__(self, mutate: bool, ask: Callable[[str], str] = input) -> None:
        self.mutate = mutate
        self.ask = ask

    @staticmethod
    def _gh(*args: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        cmd = ["gh", "api", "-H", "X-GitHub-Api-Version: 2022-11-28", *args]
        if body is not None:
            cmd += ["--input", "-"]
        proc = subprocess.run(  # noqa: S603 - fixed argv, gh signed in by the operator
            cmd,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        out = proc.stdout.strip()
        try:
            data = json.loads(out) if out else None
        except json.JSONDecodeError:
            data = out
        return proc.returncode, data

    def _branch(self, repo: str) -> str:
        _, body = self._gh(f"repos/{repo}")
        return str(body["default_branch"])

    def _file(self, repo: str, path: str, ref: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(path)
        code, body = self._gh(f"repos/{repo}/contents/{quoted}?ref={ref}")
        if code != 0 or not isinstance(body, dict):
            raise SetupError(f"cannot read {path}@{ref[:12]} with gh")
        return body

    def read_file(self, repo: str, path: str, ref: str) -> bytes:
        body = self._file(repo, path, ref)
        return base64.b64decode(body["content"])

    def rename(self, repo: str, path: str, new_path: str) -> None:
        if not self.mutate:
            self.ask(f"On GitHub, rename {path} to {new_path} in {repo} and commit; press Enter: ")
            return
        branch = self._branch(repo)
        old = self._file(repo, path, branch)
        content = old["content"].replace("\n", "")
        code, _ = self._gh(
            "-X", "PUT", f"repos/{repo}/contents/{urllib.parse.quote(new_path)}",
            body={"message": f"acceptance: rename {path}", "content": content, "branch": branch},
        )  # fmt: skip
        if code != 0:
            raise SetupError(f"gh could not create {new_path}")
        self._delete_file(repo, path, branch, old["sha"])

    def delete(self, repo: str, path: str) -> None:
        if not self.mutate:
            self.ask(f"On GitHub, delete {path} from {repo} and commit; press Enter: ")
            return
        branch = self._branch(repo)
        self._delete_file(repo, path, branch, self._file(repo, path, branch)["sha"])

    def _delete_file(self, repo: str, path: str, branch: str, sha: str) -> None:
        code, _ = self._gh(
            "-X", "DELETE", f"repos/{repo}/contents/{urllib.parse.quote(path)}",
            body={"message": f"acceptance: delete {path}", "sha": sha, "branch": branch},
        )  # fmt: skip
        if code != 0:
            raise SetupError(f"gh could not delete {path}")

    def is_collaborator(self, repo: str, login: str) -> bool:
        code, _ = self._gh(f"repos/{repo}/collaborators/{login}")
        return code == 0  # 204 -> exit 0; 404 -> nonzero

    def remove_collaborator(self, repo: str, login: str) -> None:
        if not self.mutate:
            self.ask(f"On GitHub, remove {login} as a collaborator of {repo}; press Enter: ")
            return
        code, _ = self._gh("-X", "DELETE", f"repos/{repo}/collaborators/{login}")
        if code != 0:
            raise SetupError(f"gh could not remove {login}")


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(
        self, method: str, path: str, token: str | None = None, body: Any = None
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(  # noqa: S310 - operator-supplied local API
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                raw, status = response.read().decode(), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read().decode(), exc.code
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return status, raw

    def ask(self, token: str, question: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310
            f"{self.base}/api/v1/chat",
            data=json.dumps({"question": question, "mode": "code"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        events: list[dict[str, Any]] = []
        with urllib.request.urlopen(request, timeout=900) as response:  # noqa: S310
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
            "inference": done.get("inference"),
            "verification_label": (done.get("verification") or {}).get("label"),
            "checker": ((done.get("verification") or {}).get("checker") or {}).get("model"),
            "sentences": [e["data"]["text"] for e in events if e["event"] == "token"],
            "citations": [e["data"] for e in events if e["event"] == "citation"],
        }


# -- the run ----------------------------------------------------------------------------------


class Run:
    def __init__(self, args: argparse.Namespace, api: Api, repo: RepoAdapter) -> None:
        self.a = args
        self.api = api
        self.repo = repo
        self.evidence: dict[str, Any] = {
            "kind": "m5_github_acceptance",
            "live_github": args.live_label,
            "repository": args.repo,
            "api": args.api,
            "started_at": now(),
            "steps": [],
        }
        self.failed = False

    def step(self, name: str, ok: bool, **detail: Any) -> bool:
        self.evidence["steps"].append({"step": name, "ok": ok, "at": now(), **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)
        self.failed |= not ok
        return ok

    def login(self, email: str) -> str:
        creds = json.loads(Path(self.a.credentials).read_text())
        status, body = self.api.call(
            "POST",
            "/api/v1/auth/login",
            body={"tenant": self.a.tenant, "email": email, "password": creds[self.a.tenant][email]},
        )
        if status != 200:
            raise SetupError(f"login failed for {email}")
        return str(body["access_token"])

    def wait(self, check: Callable[[], Any], what: str) -> Any:
        deadline = time.monotonic() + self.a.timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(self.a.poll)
        raise Timeout(what)

    def sync(self, sid: str) -> dict[str, Any]:
        status, started = self.api.call("POST", f"/api/v1/admin/sources/{sid}/sync", self.admin)
        if status != 202:
            return {"status": "request_failed", "http": status}
        run_id = started["run_id"]

        def done() -> Any:
            _, run = self.api.call("GET", f"/api/v1/admin/sources/{sid}/runs/{run_id}", self.admin)
            return run if run and run.get("status") in TERMINAL else None

        return dict(self.wait(done, f"sync run {run_id}"))

    def view(self, sid: str) -> dict[str, Any]:
        _, body = self.api.call("GET", f"/api/v1/admin/sources/{sid}", self.admin)
        return dict(body or {})

    def item(self, view: dict[str, Any], path: str) -> dict[str, Any] | None:
        return next((i for i in view.get("items", []) if i["path"] == path), None)

    def ready(self, document_id: str) -> bool:
        def status() -> Any:
            _, body = self.api.call("GET", f"/api/v1/documents/{document_id}", self.admin)
            state = (body or {}).get("status")
            return state if state in ("ready", "failed", "needs_ocr") else None

        return self.wait(status, f"ingestion of {document_id}") == "ready"

    def create_source(self) -> str:
        status, body = self.api.call(
            "POST",
            "/api/v1/admin/sources",
            self.admin,
            {
                "kind": "github",
                "name": self.a.repo,
                "external_root": self.a.repo,
                "department": self.a.department,
                "classification": "internal",
                "allowed_countries": ["*"],
            },
        )
        if status != 201:
            raise SetupError(f"create source failed ({status}: {(body or {}).get('error')})")
        return str(body["source_id"])

    def opens(self, answer: dict[str, Any], cid: str) -> int:
        status, _ = self.api.call(
            "GET", f"/api/v1/runs/{answer['run_id']}/citations/{cid}", self.reader
        )
        return status

    def answer(self, name: str, question: str) -> dict[str, Any]:
        result = self.api.ask(self.reader, question)
        if self.a.require_gateway and result["inference"] not in (None, "gateway"):
            self.step(f"{name}_real_model", False, inference=result["inference"])
        return result

    def check_cited(
        self, name: str, answer: dict[str, Any], path: str, commit: str
    ) -> dict[str, Any] | None:
        """The answer is published and cites ``path``; verify that citation directly."""

        cited = [c for c in answer["citations"] if c.get("path") == path]
        if answer["outcome"] != "published" or not cited:
            self.step(name, False, answer=answer)
            return None
        citation = cited[0]
        raw = self.repo.read_file(self.a.repo, path, commit)
        problems = verify_code_citation(
            citation, repo=self.a.repo, path=path, commit=commit, raw=raw
        )
        status, opened = self.api.call(
            "GET",
            f"/api/v1/runs/{answer['run_id']}/citations/{citation['citation_id']}",
            self.reader,
        )
        if status != 200 or (opened or {}).get("integrity") != "verified":
            problems.append(f"citation reopen {status} {(opened or {}).get('integrity')}")
        elif (opened["line_start"], opened["line_end"], opened["commit"]) != (
            citation["line_start"],
            citation["line_end"],
            citation["commit"],
        ):
            problems.append("reopened citation reports different lines or commit")
        self.step(
            name,
            not problems,
            problems=problems,
            inference=answer["inference"],
            verification_label=answer["verification_label"],
            checker=answer["checker"],
            citation={
                k: citation.get(k)
                for k in (
                    "repository",
                    "path",
                    "commit",
                    "blob_sha",
                    "line_start",
                    "line_end",
                    "quote",
                    "url",
                )
            },
        )
        return citation if not problems else None

    def execute(self) -> int:  # noqa: PLR0915 - one linear acceptance script
        a = self.a
        self.admin = self.login(a.admin)
        self.reader = self.login(a.reader)
        _, users = self.api.call("GET", "/api/v1/admin/users", self.admin)
        reader_id = next((u["user_id"] for u in users or [] if u["email"] == a.reader), None)
        status, _ = self.api.call(
            "PUT",
            f"/api/v1/admin/users/{reader_id}/external-accounts",
            self.admin,
            {"github": a.login},
        )
        if not self.step("A_link_login_to_reader", status == 200, http=status):
            return 3
        if not self.step(
            "A_login_is_collaborator_on_github", self.repo.is_collaborator(a.repo, a.login)
        ):
            return 3

        # -- A: source and first sync --------------------------------------------------------
        _, sources = self.api.call("GET", "/api/v1/admin/sources", self.admin)
        existing = next(
            (
                s
                for s in sources or []
                if s["kind"] == "github" and s["external_root"].lower() == a.repo.lower()
            ),
            None,
        )
        sid = existing["source_id"] if existing else self.create_source()
        run = self.sync(sid)
        view = self.view(sid)
        c1 = view.get("external_revision")
        file_a, file_b = self.item(view, a.path), self.item(view, a.delete_path)
        ok = (
            run.get("status") == "succeeded"
            and view.get("access") == "current"
            and bool(
                file_a and file_b and file_a["state"] == "active" and file_b["state"] == "active"
            )
        )
        if not self.step("A_sync_1", ok, run=run, access=view.get("access"), commit=c1):
            return 1
        if not (file_a and file_b and c1):
            return 1
        doc_a, doc_b = file_a["document_id"], file_b["document_id"]
        if not self.step("A_ingested", self.ready(doc_a) and self.ready(doc_b)):
            return 1

        # -- B: independent citation checks ---------------------------------------------------
        ans_a = self.answer("B_answer_a", a.question)
        cit_a = self.check_cited("B_citation_a_matches_github", ans_a, a.path, c1)
        ans_b = self.answer("B_answer_b", a.delete_question)
        cit_b = self.check_cited("B_citation_b_matches_github", ans_b, a.delete_path, c1)
        if not (cit_a and cit_b):
            return 1
        if a.stop_after == "B":
            self.evidence["stopped_after"] = "B"
            return 0

        # -- C: rename and delete on GitHub, reader still a collaborator ----------------------
        self.repo.rename(a.repo, a.path, a.rename_to)
        self.repo.delete(a.repo, a.delete_path)
        run = self.sync(sid)
        view = self.view(sid)
        c2 = view.get("external_revision")
        old_a, new_a, old_b = (self.item(view, p) for p in (a.path, a.rename_to, a.delete_path))
        ok = (
            run.get("status") == "succeeded"
            and c2 != c1
            and (old_a or {}).get("state") == "removed"
            and (old_b or {}).get("state") == "removed"
            and (new_a or {}).get("state") == "active"
        )
        if not self.step(
            "C_sync_2_sees_rename_and_delete", ok, run=run, commit=c2, items=[old_a, new_a, old_b]
        ):
            return 1
        if not (new_a and c2):
            return 1
        still = self.repo.is_collaborator(a.repo, a.login)
        self.step("C_reader_still_collaborator", still)
        self.step(
            "C_old_citations_inaccessible",
            self.opens(ans_a, cit_a["citation_id"]) == 404
            and self.opens(ans_b, cit_b["citation_id"]) == 404,
        )
        self.ready(new_a["document_id"])
        again_b = self.answer("C_answer_b_after_delete", a.delete_question)
        cited_ids = {c["document_id"] for c in again_b["citations"]}
        self.step(
            "C_deleted_file_cannot_contribute",
            doc_b not in cited_ids
            and doc_a not in cited_ids
            and cit_b["quote"] not in " ".join(again_b["sentences"]),
            outcome=again_b["outcome"],
            abstention=again_b["abstention"],
        )
        again_a = self.answer("C_answer_a_after_rename", a.question)
        self.step(
            "C_old_path_cannot_contribute",
            doc_a not in {c["document_id"] for c in again_a["citations"]},
        )
        cit_renamed = self.check_cited(
            "C_renamed_file_cited_at_new_commit", again_a, a.rename_to, c2
        )
        if self.failed or not cit_renamed:
            return 1
        if a.stop_after == "C":
            self.evidence["stopped_after"] = "C"
            return 0

        # -- D: source deletion with the reader still a collaborator --------------------------
        status, _ = self.api.call(
            "PATCH", f"/api/v1/admin/sources/{sid}", self.admin, {"status": "deleted"}
        )
        self.step("D_delete_source", status == 200, http=status)
        self.step("D_reader_still_collaborator", self.repo.is_collaborator(a.repo, a.login))
        after = self.answer("D_answer_after_source_deletion", a.question)
        self.step(
            "D_deleted_source_cannot_contribute",
            new_a["document_id"] not in {c["document_id"] for c in after["citations"]}
            and after["outcome"] != "published",
            outcome=after["outcome"],
            abstention=after["abstention"],
        )
        self.step(
            "D_old_citation_inaccessible", self.opens(again_a, cit_renamed["citation_id"]) == 404
        )
        sid2 = self.create_source()
        run = self.sync(sid2)
        view = self.view(sid2)
        renamed = self.item(view, a.rename_to)
        self.step(
            "D_recreated_source_syncs", run.get("status") == "succeeded" and bool(renamed), run=run
        )
        if self.failed or not renamed:
            return 1
        self.ready(renamed["document_id"])
        restored = self.answer("D_answer_from_recreated_source", a.question)
        cit_restored = self.check_cited(
            "D_reader_answered_again_from_new_source",
            restored,
            a.rename_to,
            str(view["external_revision"]),
        )
        if not cit_restored:
            return 1
        if a.stop_after == "D":
            self.evidence["stopped_after"] = "D"
            return 0

        # -- E: collaborator revocation ------------------------------------------------------
        self.repo.remove_collaborator(a.repo, a.login)
        self.step("E_login_removed_on_github", not self.repo.is_collaborator(a.repo, a.login))
        run = self.sync(sid2)
        removed = (run.get("stats") or {}).get("collaborators_removed", 0)
        self.step(
            "E_sync_detects_removal", run.get("status") == "succeeded" and removed >= 1, run=run
        )
        revoked = self.answer("E_answer_after_revocation", a.question)
        self.step(
            "E_revoked_reader_refused",
            revoked["abstention"] == "no_sources" and not revoked["citations"],
            outcome=revoked["outcome"],
        )
        self.step(
            "E_old_citation_inaccessible", self.opens(restored, cit_restored["citation_id"]) == 404
        )
        return 1 if self.failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--repo", required=True, help="owner/name of the dedicated test repository")
    parser.add_argument("--login", required=True, help="second GitHub account, read collaborator")
    parser.add_argument("--path", default=DEFAULTS["path"])
    parser.add_argument("--question", default=DEFAULTS["question"])
    parser.add_argument("--rename-to", default=DEFAULTS["rename_to"])
    parser.add_argument("--delete-path", default=DEFAULTS["delete_path"])
    parser.add_argument("--delete-question", default=DEFAULTS["delete_question"])
    parser.add_argument("--tenant", default="niletech")
    parser.add_argument("--reader", default="finance.reader@niletech.example")
    parser.add_argument("--admin", default="access.admin@niletech.example")
    parser.add_argument("--department", default="it")
    parser.add_argument("--credentials", default=str(ROOT / "var" / "dev-credentials.json"))
    parser.add_argument("--mutate", choices=("manual", "gh"), default="manual")
    parser.add_argument(
        "--require-gateway", action="store_true", help="fail unless answers used the real model"
    )
    parser.add_argument("--stop-after", choices=("B", "C", "D"), default=None)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--poll", type=float, default=2)
    parser.add_argument("--out", default=None)
    parser.add_argument("--live-label", default="live", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, repo: RepoAdapter | None = None) -> int:
    args = parse_args(argv)
    run = Run(args, Api(args.api), repo or GhRepo(mutate=args.mutate == "gh"))
    out = Path(args.out) if args.out else (
        ROOT / "var" / f"m5-github-acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )  # fmt: skip
    try:
        code = run.execute()
    except Timeout as exc:
        run.evidence["timeout"] = str(exc)
        code = 2
    except (SetupError, OSError, KeyError) as exc:
        run.evidence["setup_error"] = f"{type(exc).__name__}: {exc}"
        code = 3
    run.evidence["finished_at"] = now()
    run.evidence["result"] = {0: "pass", 1: "fail", 2: "timeout", 3: "setup_error"}[code]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run.evidence, indent=2) + "\n")
    print(f"evidence: {out} ({run.evidence['result']})")
    return code


if __name__ == "__main__":
    sys.exit(main())
