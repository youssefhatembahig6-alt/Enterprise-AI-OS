#!/usr/bin/env python3
"""M8 browser check of the chat UI with automatic routing (Playwright, headless Chromium).

The composer has no source picker: every question is routed by the server's orchestrator,
and each answer must carry the label of the handler that replied. Checks:

  status_badge     the header shows Connected · Qwen3-8B-AWQ (gateway) or FAKE model.
  no_source_picker the composer has no Source control.
  hello            "hello" gets a natural General reply (label General, no citations).
  general          a fresh open-ended question: label General, published, uncited.
  documents_leave  a fresh annual-leave question: label Documents, published with a
                   citation whose passage opens ("text hash verified") through
                   GET /api/v1/runs/{id}/citations/{cid} (access re-checked).
  sales_report     a sales question: label Sales report (SQL), a verified table, and the
                   "Email this report for approval" action opening the approval form for
                   that report (nothing is drafted or sent here).
  clarify          a vague request ("the numbers?") gets one short clarifying question,
                   labelled as such, with no run.
  revalidation     same session, no reload: finance.reader gets a cited answer from the
                   finance commentary and opens the citation, switches to a new chat, an
                   administrator removes finance.reader from finance-team, and the chat is
                   reopened from Recents. While the replay is held open the old answer, its
                   citation and its passage are hidden ("Re-checking your access…"); after
                   it, the answer ends with access_revoked and the citation endpoint
                   returns 404. Membership is restored (also on failure) and a further
                   reopen shows the answer again: revalidation runs on every reopen.
  sales_refusal    sales.reader asks for restricted finance commentary: the documents
                   handler's no-sources refusal, no restricted figures on the page.
  history          the chats survive a reload and a new sign-in under Recents; a chat
                   reopens with its answers replayed (labels intact); Delete asks for
                   confirmation, removes it, and it stays gone after another reload; a
                   second employee cannot list, open or delete the first one's chat
                   (API 404) and does not see it in Recents.

For every run the script reads GET /api/v1/runs/{id} with the user's own token and
records ``inference_mode`` and the served chat model. With ``--expect-inference gateway``
the published answers must say ``gateway`` with the pinned revision and an actual model
call; with ``fake`` they must say ``fake`` (local plumbing check only, never model
evidence).

    python scripts/m8_chat_ui_check.py --web http://127.0.0.1:13000 \\
        --api http://127.0.0.1:18000 --credentials <dev-credentials.json> \\
        --out <new empty dir> --run-id <id> --expect-inference gateway

Passwords come from the credentials file and are typed into the page; they are never
printed or written to the evidence. Exit status 0 only if every check passes. ``--out``
must be new or empty (exit 2 otherwise), and the report records ``--run-id``, so a caller
accepts a run only on exit 0 plus a passing report carrying its own run id.
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

from playwright.sync_api import Page, Response, sync_playwright

TENANT = "niletech"
FINANCE = "finance.reader@niletech.example"
SALES = "sales.reader@niletech.example"
ADMIN = "access.admin@niletech.example"
PINNED_CHAT = ("Qwen/Qwen3-8B-AWQ", "4da05a8edb55c6046cce958586c33b61da07bb79")
LEAVE_Q = "If I don't use all my vacation this year, how many days can I roll over, and until when?"
RESTRICTED_Q = "Why did collections lag revenue in Q1 2026?"
RESTRICTED_MARKERS = ("370 open invoices", "NILETECH-FINANCE-SECRET")
GENERAL_Q = (
    "Suggest three practical ways a small team can make its weekly status meeting shorter "
    "and more useful."
)
REFUSAL = "I couldn't find information in documents you can access that answers this."
HELLO = "hello"
SALES_Q = "What were fulfilled sales in Q1 2026?"
VAGUE_Q = "the numbers?"
CHECKS = (
    "status_badge",
    "no_source_picker",
    "hello",
    "general",
    "documents_leave",
    "sales_report",
    "clarify",
    "revalidation",
    "sales_refusal",
    "history",
)


def api_json(api: str, path: str, token: str | None = None, body: Any = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - local stack
        api + path, data=data, headers=headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read() or b"null")


def api_status(api: str, path: str, token: str, method: str = "GET") -> int:
    req = urllib.request.Request(  # noqa: S310 - local stack
        api + path, headers={"Authorization": f"Bearer {token}"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def api_put(api: str, path: str, token: str, body: Any) -> Any:
    req = urllib.request.Request(  # noqa: S310 - local stack
        api + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read() or b"null")


def set_groups(api: str, admin_token: str, email: str, change: Any) -> list[str]:
    """Change a user's groups through the admin API; returns the groups before."""

    users = api_json(api, "/api/v1/admin/users", admin_token)
    user = next(u for u in users if u["email"] == email)
    before = list(user["groups"])
    api_put(
        api,
        f"/api/v1/admin/users/{user['user_id']}/groups",
        admin_token,
        {"groups": change(before)},
    )
    return before


def conversation_ids(api: str, token: str) -> set[str]:
    listed = api_json(api, "/api/v1/conversations", token)["conversations"]
    return {c["conversation_id"] for c in listed}


class Ui:
    def __init__(self, page: Page, web: str, out: Path) -> None:
        self.page = page
        self.web = web
        self.out = out
        self.chats: list[dict[str, str | None]] = []
        self.citation_calls: list[tuple[str, int]] = []
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if url.endswith("/api/v1/chat") and response.request.method == "POST":
            self.chats.append(
                {
                    "run_id": response.headers.get("x-run-id"),
                    "handler": response.headers.get("x-handler"),
                    "conversation_id": response.headers.get("x-conversation-id"),
                }
            )
        elif "/citations/" in url and response.request.method == "GET":
            self.citation_calls.append((url.split("/api/v1", 1)[-1], response.status))

    def shot(self, name: str) -> str:
        path = self.out / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=False)
        return path.name

    def sign_in(self, email: str, password: str) -> None:
        p = self.page
        p.goto(self.web)
        p.get_by_label("Organization").fill(TENANT)
        p.get_by_label("Email").fill(email)
        p.get_by_label("Password").fill(password)
        p.get_by_role("button", name="Sign in").click()
        p.get_by_text("What can I find for you today").wait_for(timeout=30_000)

    def sign_out(self) -> None:
        p = self.page
        p.get_by_role("button", name="Account menu").click()
        p.get_by_role("menuitem", name="Sign out").click()
        p.get_by_role("button", name="Sign in").wait_for(timeout=15_000)

    def ask(self, question: str, timeout_s: float) -> dict[str, Any]:
        """Type into the visible composer and press Enter (no source is chosen: the server
        routes the question); wait until the answer has finished."""

        p = self.page
        before = len(self.chats)
        composer = p.locator("form.composer").last  # the chat composer, not the report form
        box = composer.get_by_label("Your question")
        box.fill(question)
        box.press("Enter")
        deadline = time.monotonic() + timeout_s
        answer = p.locator(".convo .a").last
        while time.monotonic() < deadline:
            if (
                answer.locator(".verify").count()
                or answer.locator(".errbox").count()
                or answer.locator("p.clarify").count()
            ):
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(f"no finished answer after {timeout_s:.0f} s")
        chat = self.chats[before] if len(self.chats) > before else {}
        return {
            "run_id": chat.get("run_id"),
            "handler_header": chat.get("handler"),
            "conversation_id": chat.get("conversation_id"),
            "label": answer.locator(".chip.handler").all_inner_texts(),
            "text": answer.inner_text(),
            "reply": answer.locator(".body > p:not(.note):not(.refuse)").all_inner_texts(),
            "pills": answer.locator(".vpill").all_inner_texts(),
            "error": answer.locator(".errbox").all_inner_texts(),
            "citations": answer.locator("button.cite").count(),
        }

    def recents(self) -> list[str]:
        rows = self.page.locator(".side .recent[data-conversation]")
        return [rows.nth(i).get_attribute("data-conversation") or "" for i in range(rows.count())]


def labelled(ui: dict[str, Any], want: str) -> list[str]:
    return (
        [] if ui.get("label") == [want] else [f"answer labelled {ui.get('label')!r}, not {want!r}"]
    )


def run_record(api: str, token: str, run_id: str | None) -> dict[str, Any]:
    if not run_id:
        return {"error": "no run id captured"}
    run = api_json(api, f"/api/v1/runs/{run_id}", token)
    diag = run.get("diagnostics") or {}
    served = (diag.get("served_models") or {}).get("models", {}).get("chat")
    calls = diag.get("model_calls") or []
    return {
        "run_id": run_id,
        "status": run.get("status"),
        "inference_mode": run.get("inference_mode"),
        "outcome_code": run.get("outcome_code"),
        "served_chat": served,
        "model_calls": calls,
    }


def real(record: dict[str, Any], expect: str, purpose: str) -> list[str]:
    """The run was answered by the expected inference. For the real gateway: the pinned
    Qwen revision was verified for this run and an actual model call with ``purpose``
    is recorded (measured duration and completion tokens)."""

    problems = []
    if record.get("inference_mode") != expect:
        problems.append(f"inference_mode={record.get('inference_mode')!r}, expected {expect!r}")
    calls = [c for c in record.get("model_calls") or [] if c.get("purpose") == purpose]
    if not calls:
        problems.append(f"no {purpose!r} model call recorded")
    if expect == "gateway":
        chat = record.get("served_chat") or {}
        if (chat.get("model"), chat.get("revision")) != PINNED_CHAT:
            problems.append(f"served chat {chat!r} is not the pinned {PINNED_CHAT}")
        if calls and not any((c.get("seconds") or 0) > 0 and c.get("tokens") for c in calls):
            problems.append(f"{purpose!r} call has no measured duration or token count")
    return problems


def answer_state(page: Page) -> dict[str, Any]:
    answer = page.locator(".convo .a").last
    return {
        "label": answer.locator(".chip.handler").all_inner_texts(),
        "status": answer.locator(".step.run").all_inner_texts(),
        "reply": answer.locator(".body > p:not(.note):not(.refuse)").all_inner_texts(),
        "citations": answer.locator("button.cite").count(),
        "error": answer.locator(".errbox").all_inner_texts(),
        "passages": page.locator(".panel .pcard blockquote").count(),
    }


def revalidation(  # noqa: PLR0915 - one scripted scenario
    page: Page, ui: Ui, args: argparse.Namespace, creds: dict[str, str], tokens: dict[str, str]
) -> dict[str, Any]:
    problems: list[str] = []
    page.get_by_role("button", name="New chat").click()
    cited = ui.ask(RESTRICTED_Q, args.timeout)
    chat, run_id = cited["conversation_id"], cited["run_id"]
    old_reply = [t for t in cited["reply"] if t.strip()]
    if cited["label"] != ["Documents"] or not cited["citations"] or not old_reply:
        problems.append(
            f"no cited Documents answer to start from: {cited['label']} {cited['citations']}"
        )
        return {"ui": cited, "problems": problems, "pass": False}
    page.locator(".convo .a").last.locator("button.cite").first.click()
    page.get_by_text("text hash verified").first.wait_for(timeout=30_000)
    citation_paths = [path for path, code in ui.citation_calls if run_id in path and code == 200]
    before_shot = ui.shot("7a-revalidation-cited")
    page.get_by_role("button", name="Close panel").click()

    page.get_by_role("button", name="New chat").click()  # switch away
    admin = api_json(
        args.api,
        "/api/v1/auth/login",
        body={"tenant": TENANT, "email": ADMIN, "password": creds[ADMIN]},
    )["access_token"]
    original = set_groups(args.api, admin, FINANCE, lambda g: [x for x in g if x != "finance-team"])
    held: list[Any] = []
    pattern = f"**/api/v1/runs/{run_id}/events**"
    during: dict[str, Any] = {}
    after: dict[str, Any] = {}
    restored: dict[str, Any] = {}
    try:
        page.route(pattern, lambda route: held.append(route))
        row = page.locator(f".side .recent[data-conversation='{chat}'] button.row")
        row.click()
        deadline = time.monotonic() + 30
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        during = answer_state(page)
        during["replay_held"] = bool(held)
        during["page_has_old_text"] = any(t in page.inner_text("body") for t in old_reply)
        during["screenshot"] = ui.shot("7b-revalidation-during")
        if not held:
            problems.append("the reopen did not replay the answer from the server")
        if during["page_has_old_text"] or during["citations"] or during["passages"]:
            problems.append(f"old answer visible while re-checking: {during}")
        if not any("Re-checking your access" in t for t in during["status"]):
            problems.append(f"no re-checking state shown: {during['status']}")
        for route in held:
            route.continue_()
        page.unroute(pattern)
        deadline = time.monotonic() + 60
        answer = page.locator(".convo .a").last
        while time.monotonic() < deadline and not (
            answer.locator(".errbox").count() or answer.locator(".verify").count()
        ):
            page.wait_for_timeout(250)
        after = answer_state(page)
        after["page_has_old_text"] = any(t in page.inner_text("body") for t in old_reply)
        after["leaked_markers"] = [m for m in RESTRICTED_MARKERS if m in page.content()]
        after["screenshot"] = ui.shot("7c-revalidation-after")
        after["citation_status"] = [
            api_status(args.api, f"/api/v1{path}", tokens[FINANCE]) for path in citation_paths
        ]
        if after["page_has_old_text"] or after["citations"] or after["passages"]:
            problems.append(f"old answer came back after re-checking: {after}")
        if after["leaked_markers"]:
            problems.append(f"restricted content on the page: {after['leaked_markers']}")
        if not any("access_revoked" in t or "no longer have access" in t for t in after["error"]):
            problems.append(f"no access_revoked after re-checking: {after['error']}")
        if not citation_paths or any(code != 404 for code in after["citation_status"]):
            problems.append(f"citation endpoint after revocation: {after['citation_status']}")
    finally:
        set_groups(args.api, admin, FINANCE, lambda g: original)
    # Reopen again with access restored: revalidation runs every time, so it comes back.
    page.get_by_role("button", name="New chat").click()
    page.locator(f".side .recent[data-conversation='{chat}'] button.row").click()
    answer = page.locator(".convo .a").last
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (
        answer.locator(".errbox").count() or answer.locator(".verify").count()
    ):
        page.wait_for_timeout(250)
    restored = answer_state(page)
    restored["screenshot"] = ui.shot("7d-revalidation-restored")
    if not restored["citations"] or not any(t.strip() for t in restored["reply"]):
        problems.append(f"answer did not come back after access was restored: {restored}")
    return {
        "question": RESTRICTED_Q,
        "chat": chat,
        "run_id": run_id,
        "cited": {**cited, "screenshot": before_shot},
        "revoked": "finance.reader removed from finance-team by access.admin",
        "during": during,
        "after": after,
        "restored": restored,
        "problems": problems,
        "pass": not problems,
    }


def main() -> int:  # noqa: PLR0915
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--web", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--credentials", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--expect-inference", choices=["gateway", "fake"], required=True)
    ap.add_argument("--timeout", type=float, default=300.0, help="per question (s)")
    ap.add_argument(
        "--documents-question", default=LEAVE_Q, help="annual-leave question (fresh wording)"
    )
    ap.add_argument("--general-question", default=GENERAL_Q, help="open-ended General question")
    ap.add_argument(
        "--run-id",
        default=None,
        help="written into the report so the caller can tell it came from this run",
    )
    args = ap.parse_args()
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        # Fresh evidence only: an old report in this folder could be mistaken for this run's.
        print(f"--out {out} is not empty; use a new folder for every check", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    creds = json.loads(Path(args.credentials).read_text())[TENANT]
    tokens = {
        u: api_json(
            args.api,
            "/api/v1/auth/login",
            body={"tenant": TENANT, "email": u, "password": creds[u]},
        )["access_token"]
        for u in (FINANCE, SALES)
    }
    status = api_json(args.api, "/api/v1/model/status", tokens[FINANCE])
    result: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "web": args.web,
        "api": args.api,
        "expect_inference": args.expect_inference,
        "model_status": status,
        "checks": {},
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        ui = Ui(page, args.web, out)
        checks: dict[str, Any] = result["checks"]
        try:
            ui.sign_in(FINANCE, creds[FINANCE])
            result["model_chip"] = page.locator(".scope .chip").all_inner_texts()
            # The header's model status button: Connected only after the backend's
            # authenticated readiness check of the pinned Qwen chat server.
            want = (
                "Connected · Qwen3-8B-AWQ" if args.expect_inference == "gateway" else "FAKE model"
            )
            badge = page.locator("button.chip.status")
            label = ""
            for _ in range(120):
                label = badge.inner_text().strip()
                if label == want:
                    break
                time.sleep(0.5)
            checks["status_badge"] = {
                "label": label,
                "title": badge.get_attribute("title"),
                "screenshot": ui.shot("0-status-badge"),
                "problems": [] if label == want else [f"badge shows {label!r}, expected {want!r}"],
                "pass": label == want,
            }
            pickers = page.locator("form.composer select, form.composer [aria-label='Source']")
            checks["no_source_picker"] = {
                "source_controls": pickers.count(),
                "problems": []
                if pickers.count() == 0
                else ["the composer still has a Source control"],
                "pass": pickers.count() == 0,
            }

            # hello, then a fresh general question in the same chat.
            hello = ui.ask(HELLO, args.timeout)
            hello_rec = run_record(args.api, tokens[FINANCE], hello["run_id"])
            hello["screenshot"] = ui.shot("1-hello")
            problems = labelled(hello, "General") + real(
                hello_rec, args.expect_inference, "general"
            )
            if hello["handler_header"] != "general":
                problems.append(f"routed to {hello['handler_header']!r}")
            if not any(t.strip() for t in hello["reply"]):
                problems.append("empty reply")
            if hello["citations"]:
                problems.append("hello reply has citations")
            checks["hello"] = {
                "question": HELLO,
                "ui": hello,
                "run": hello_rec,
                "problems": problems,
                "pass": not problems,
            }
            history_chat = hello["conversation_id"]

            gen = ui.ask(args.general_question, args.timeout)
            gen_rec = run_record(args.api, tokens[FINANCE], gen["run_id"])
            gen["screenshot"] = ui.shot("2-general")
            problems = labelled(gen, "General") + real(gen_rec, args.expect_inference, "general")
            if gen_rec.get("outcome_code") != "general_model_reply_unverified":
                problems.append(f"outcome_code {gen_rec.get('outcome_code')!r}")
            if gen["citations"]:
                problems.append("general reply has citations")
            if not any(t.strip() for t in gen["reply"]):
                problems.append("general reply is empty")
            if args.expect_inference == "gateway" and any("FAKE" in t for t in gen["reply"]):
                problems.append("general reply carries a FAKE label")
            if gen["conversation_id"] != history_chat:
                problems.append("the follow-up was not saved to the same chat")
            checks["general"] = {
                "question": args.general_question,
                "ui": gen,
                "run": gen_rec,
                "problems": problems,
                "pass": not problems,
            }

            # documents: fresh leave question in a new chat, citation opens.
            page.get_by_role("button", name="New chat").click()
            leave = ui.ask(args.documents_question, args.timeout)
            opened: dict[str, Any] = {}
            if leave["citations"]:
                page.locator(".convo .a").last.locator("button.cite").first.click()
                page.get_by_text("text hash verified").first.wait_for(timeout=30_000)
                opened = {
                    "passage": page.locator(".panel .pcard.on blockquote").first.inner_text(),
                    "badge": "text hash verified",
                }
            leave_rec = run_record(args.api, tokens[FINANCE], leave["run_id"])
            leave["screenshot"] = ui.shot("3-documents-leave")
            problems = labelled(leave, "Documents") + real(
                leave_rec, args.expect_inference, "answer"
            )
            if leave_rec.get("status") != "published":
                problems.append(f"run status {leave_rec.get('status')!r}")
            if not leave["citations"]:
                problems.append("no citation in the reply")
            if not opened:
                problems.append("citation passage did not open")
            if not any(code == 200 for _, code in ui.citation_calls):
                problems.append("no 200 from the citation endpoint")
            checks["documents_leave"] = {
                "question": args.documents_question,
                "ui": leave,
                "opened": opened,
                "citation_requests": ui.citation_calls[:],
                "run": leave_rec,
                "problems": problems,
                "pass": not problems,
            }
            page.get_by_role("button", name="Close panel").click()

            # sales report: verified table and the email-for-approval action.
            page.get_by_role("button", name="New chat").click()
            sales = ui.ask(SALES_Q, args.timeout)
            sales_rec = run_record(args.api, tokens[FINANCE], sales["run_id"])
            answer = page.locator(".convo .a").last
            table_rows = answer.locator("table.res tbody tr").count()
            email = answer.get_by_role("button", name="Email this report for approval")
            sales["table_rows"] = table_rows
            sales["email_action"] = email.count()
            sales["screenshot"] = ui.shot("4-sales-report")
            problems = labelled(sales, "Sales report (SQL)") + real(
                sales_rec, args.expect_inference, "sql"
            )
            if sales_rec.get("status") != "published":
                problems.append(f"run status {sales_rec.get('status')!r}")
            if not table_rows:
                problems.append("no verified table")
            if not any("Numbers match query rows" in p for p in sales["pills"]):
                problems.append("figures not shown as verified")
            approval_form = ""
            if email.count():
                email.click()
                form = page.get_by_text("Email “", exact=False).first
                form.wait_for(timeout=15_000)
                approval_form = form.inner_text()
                sales["approval_screenshot"] = ui.shot("5-email-approval-form")
                page.get_by_role("button", name="Ask").first.click()
            else:
                problems.append("no 'Email this report for approval' action")
            if email.count() and "Q1 2026" not in approval_form:
                problems.append(f"approval form is not for this report: {approval_form!r}")
            sales["approval_form"] = approval_form
            checks["sales_report"] = {
                "question": SALES_Q,
                "ui": sales,
                "run": sales_rec,
                "problems": problems,
                "pass": not problems,
            }

            # clarify: a vague request gets a short question back and starts no run.
            page.get_by_role("button", name="New chat").click()
            vague = ui.ask(VAGUE_Q, args.timeout)
            clarify_text = page.locator(".convo .a").last.locator("p.clarify").all_inner_texts()
            vague["clarifying_question"] = clarify_text
            vague["screenshot"] = ui.shot("6-clarify")
            problems = labelled(vague, "Clarifying question")
            if vague["handler_header"] != "clarify":
                problems.append(f"routed to {vague['handler_header']!r}")
            if vague["run_id"]:
                problems.append("a clarifying question started a run")
            if not clarify_text or "?" not in clarify_text[0]:
                problems.append("no clarifying question shown")
            checks["clarify"] = {
                "question": VAGUE_Q,
                "ui": vague,
                "problems": problems,
                "pass": not problems,
            }

            # revalidation: same session, no reload. Cited answer, switch away, revoke,
            # reopen: the old answer stays hidden while it is re-checked and afterwards.
            checks["revalidation"] = revalidation(page, ui, args, creds, tokens)

            # history: reload (the token is memory-only, so sign in again), reopen, delete.
            problems = []
            page.reload()
            ui.sign_in(FINANCE, creds[FINANCE])
            page.locator(f".side .recent[data-conversation='{history_chat}']").wait_for(
                timeout=15_000
            )
            after_reload = ui.recents()
            for chat in {history_chat, leave["conversation_id"], sales["conversation_id"]}:
                if chat not in after_reload:
                    problems.append(f"chat {chat} missing from Recents after reload")
            row = page.locator(f".side .recent[data-conversation='{history_chat}']")
            row.locator("button.row").click()
            thread = page.locator(".convo .a")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and (
                thread.count() < 2 or thread.locator(".verify").count() < 2
            ):
                time.sleep(0.5)
            reopened = {
                "questions": page.locator(".convo .u").all_inner_texts(),
                "labels": thread.locator(".chip.handler").all_inner_texts(),
                "replies": [
                    thread.nth(i).locator(".body > p:not(.note):not(.refuse)").all_inner_texts()
                    for i in range(thread.count())
                ],
                "screenshot": ui.shot("8-history-reopened"),
            }
            if reopened["questions"] != [HELLO, args.general_question]:
                problems.append(f"reopened questions {reopened['questions']!r}")
            if reopened["labels"] != ["General", "General"]:
                problems.append(f"reopened labels {reopened['labels']!r}")
            if not all(any(t.strip() for t in r) for r in reopened["replies"]):
                problems.append("a reopened answer is empty")

            # A second employee cannot list, open or delete it.
            sales_token = tokens[SALES]
            other = {
                "listed": history_chat in conversation_ids(args.api, sales_token),
                "open": api_status(args.api, f"/api/v1/conversations/{history_chat}", sales_token),
                "delete": api_status(
                    args.api, f"/api/v1/conversations/{history_chat}", sales_token, "DELETE"
                ),
            }
            if other != {"listed": False, "open": 404, "delete": 404}:
                problems.append(f"second employee reached the chat: {other}")
            if history_chat not in conversation_ids(args.api, tokens[FINANCE]):
                problems.append("the owner lost the chat after the second employee's attempts")

            # Delete with confirmation; gone now and after another reload.
            row.hover()
            row.get_by_role("button", name="Delete chat:").click()
            confirm = page.get_by_role("group", name="Delete chat:")
            confirm.get_by_role("button", name="Delete", exact=True).click()
            page.locator(f".side .recent[data-conversation='{history_chat}']").wait_for(
                state="detached", timeout=15_000
            )
            ui.shot("9-history-deleted")
            gone_api = history_chat not in conversation_ids(args.api, tokens[FINANCE])
            page.reload()
            ui.sign_in(FINANCE, creds[FINANCE])
            time.sleep(1.5)
            gone_ui = history_chat not in ui.recents()
            if not gone_api:
                problems.append("the deleted chat is still listed by the API")
            if not gone_ui:
                problems.append("the deleted chat came back after reload")
            ui.sign_out()

            # restricted question as an employee without access; finance's chats are not shown.
            ui.sign_in(SALES, creds[SALES])
            time.sleep(1.5)
            sales_recents = ui.recents()
            leaked_chats = {leave["conversation_id"], sales["conversation_id"]} & set(sales_recents)
            if leaked_chats:
                problems.append(f"second employee sees the first one's chats: {leaked_chats}")
            checks["history"] = {
                "chat": history_chat,
                "recents_after_reload": after_reload,
                "reopened": reopened,
                "second_employee": other,
                "deleted": {"api": gone_api, "after_reload": gone_ui},
                "second_employee_recents": sales_recents,
                "problems": problems,
                "pass": not problems,
            }
            refused = ui.ask(RESTRICTED_Q, args.timeout)
            ref_rec = run_record(args.api, tokens[SALES], refused["run_id"])
            refused["screenshot"] = ui.shot("10-sales-refusal")
            problems = labelled(refused, "Documents")
            if REFUSAL not in refused["text"]:
                problems.append("the reply is not the no-sources refusal")
            leaked = [m for m in RESTRICTED_MARKERS if m in page.content()]
            if leaked:
                problems.append(f"restricted content on the page: {leaked}")
            if ref_rec.get("inference_mode") != args.expect_inference:
                problems.append(f"inference_mode={ref_rec.get('inference_mode')!r}")
            checks["sales_refusal"] = {
                "question": RESTRICTED_Q,
                "ui": refused,
                "run": ref_rec,
                "problems": problems,
                "pass": not problems,
            }
            ui.sign_out()
        finally:
            browser.close()
    result["finished_at"] = datetime.now(UTC).isoformat()
    result["pass"] = set(result["checks"]) == set(CHECKS) and all(
        c["pass"] for c in result["checks"].values()
    )
    (out / "m8-chat-ui-check.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    )
    for name, check in result["checks"].items():
        print(f"{name}: {'PASS' if check['pass'] else 'FAIL ' + '; '.join(check['problems'])}")
    print(
        "UI check", "PASSED" if result["pass"] else "FAILED", f"-> {out / 'm8-chat-ui-check.json'}"
    )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
