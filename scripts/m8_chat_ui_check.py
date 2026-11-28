#!/usr/bin/env python3
"""M8 browser check of the chat UI (Playwright, headless Chromium).

Drives the built web app like a user and records evidence:

  1. documents  finance.reader asks a newly phrased annual-leave question with the
                **Documents** source; the reply must be published with a citation, and
                clicking the citation must open the passage ("text hash verified")
                through GET /api/v1/runs/{id}/citations/{cid} (access re-checked).
  2. refusal    sales.reader asks for the restricted Q1 finance commentary content with
                the **Documents** source; the reply must be the no-sources refusal and
                the page must not show the restricted figures.
  3. general    finance.reader sends a fresh open-ended prompt with the **General**
                source; the reply must be published and labelled as a general reply.

For every run the script reads GET /api/v1/runs/{id} with the user's own token and
records ``inference_mode`` and the served chat model. With ``--expect-inference gateway``
the successful runs (1 and 3) must say ``gateway`` with the pinned revision; with
``fake`` they must say ``fake`` (local plumbing check only, never model evidence).

    python scripts/m8_chat_ui_check.py --web http://127.0.0.1:13000 \\
        --api http://127.0.0.1:18000 --credentials <dev-credentials.json> \\
        --out <dir> --expect-inference gateway

Passwords come from the credentials file and are typed into the page; they are never
printed or written to the evidence. Exit status 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Response, sync_playwright

TENANT = "niletech"
FINANCE = "finance.reader@niletech.example"
SALES = "sales.reader@niletech.example"
PINNED_CHAT = ("Qwen/Qwen3-8B-AWQ", "4da05a8edb55c6046cce958586c33b61da07bb79")
LEAVE_Q = "If I don't use all my vacation this year, how many days can I roll over, and until when?"
RESTRICTED_Q = "Why did collections lag revenue in Q1 2026?"
RESTRICTED_MARKERS = ("370 open invoices", "NILETECH-FINANCE-SECRET")
GENERAL_Q = (
    "Suggest three practical ways a small team can make its weekly status meeting shorter "
    "and more useful."
)
REFUSAL = "I couldn't find information in documents you can access that answers this."


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


class Ui:
    def __init__(self, page: Page, web: str, out: Path) -> None:
        self.page = page
        self.web = web
        self.out = out
        self.run_ids: list[str] = []
        self.citation_calls: list[tuple[str, int]] = []
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if url.endswith("/api/v1/chat") and response.request.method == "POST":
            run_id = response.headers.get("x-run-id")
            if run_id:
                self.run_ids.append(run_id)
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

    def ask(self, question: str, source_label: str, timeout_s: float) -> dict[str, Any]:
        """Type into the visible composer, pick the Source, press Enter, wait for the end."""

        p = self.page
        before = len(self.run_ids)
        composer = p.locator("form.composer").last  # the chat composer, not the hidden report form
        box = composer.get_by_label("Your question")
        composer.get_by_label("Source").select_option(label=source_label)
        box.fill(question)
        box.press("Enter")
        deadline = time.monotonic() + timeout_s
        answer = p.locator(".a").last
        while time.monotonic() < deadline:
            if answer.locator(".verify").count() or answer.locator(".errbox").count():
                break
            time.sleep(0.5)
        else:
            raise TimeoutError(f"no finished answer after {timeout_s:.0f} s")
        return {
            "run_id": self.run_ids[before] if len(self.run_ids) > before else None,
            "text": answer.inner_text(),
            "pills": answer.locator(".vpill").all_inner_texts(),
            "error": answer.locator(".errbox").all_inner_texts(),
            "citations": answer.locator("button.cite").count(),
        }


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
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
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
        try:
            # 1. documents: newly phrased leave question, cited, passage opens.
            ui.sign_in(FINANCE, creds[FINANCE])
            result["model_chip"] = page.locator(".scope .chip").all_inner_texts()
            # 0. the header's model status button: Connected only after the backend's
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
            result["checks"]["status_badge"] = {
                "label": label,
                "title": badge.get_attribute("title"),
                "screenshot": ui.shot("0-status-badge"),
                "problems": [] if label == want else [f"badge shows {label!r}, expected {want!r}"],
                "pass": label == want,
            }
            leave = ui.ask(args.documents_question, "Documents you can access", args.timeout)
            opened: dict[str, Any] = {}
            if leave["citations"]:
                page.locator(".a").last.locator("button.cite").first.click()
                page.get_by_text("text hash verified").first.wait_for(timeout=30_000)
                opened = {
                    "passage": page.locator(".panel .pcard.on blockquote").first.inner_text(),
                    "badge": "text hash verified",
                }
            leave_rec = run_record(args.api, tokens[FINANCE], leave["run_id"])
            leave["screenshot"] = ui.shot("1-documents-leave")
            problems = real(leave_rec, args.expect_inference, "answer")
            if leave_rec.get("status") != "published":
                problems.append(f"run status {leave_rec.get('status')!r}")
            if not leave["citations"]:
                problems.append("no citation in the reply")
            if not opened:
                problems.append("citation passage did not open")
            if not any(code == 200 for _, code in ui.citation_calls):
                problems.append("no 200 from the citation endpoint")
            result["checks"]["documents_leave"] = {
                "question": args.documents_question,
                "ui": leave,
                "opened": opened,
                "citation_requests": ui.citation_calls[:],
                "run": leave_rec,
                "problems": problems,
                "pass": not problems,
            }
            # 3. general: fresh open-ended prompt.
            page.get_by_role("button", name="New chat").click()
            gen = ui.ask(args.general_question, "General (no company data)", args.timeout)
            gen_rec = run_record(args.api, tokens[FINANCE], gen["run_id"])
            gen["screenshot"] = ui.shot("3-general")
            problems = real(gen_rec, args.expect_inference, "general")
            if gen_rec.get("status") != "published":
                problems.append(f"run status {gen_rec.get('status')!r}")
            if gen_rec.get("outcome_code") != "general_model_reply_unverified":
                problems.append(f"outcome_code {gen_rec.get('outcome_code')!r}")
            if not any("General reply" in p for p in gen["pills"]):
                problems.append("reply is not labelled as general")
            if gen["citations"]:
                problems.append("general reply has citations")
            reply = (
                page.locator(".a")
                .last.locator(".body > p:not(.note):not(.refuse)")
                .all_inner_texts()
            )
            if not any(t.strip() for t in reply):
                problems.append("general reply is empty")
            if args.expect_inference == "gateway" and any("FAKE" in t for t in reply):
                problems.append("general reply carries a FAKE label")
            gen["reply"] = reply
            result["checks"]["general"] = {
                "question": args.general_question,
                "ui": gen,
                "run": gen_rec,
                "problems": problems,
                "pass": not problems,
            }
            ui.sign_out()
            # 2. refusal: sales.reader asks for restricted finance content.
            ui.sign_in(SALES, creds[SALES])
            refused = ui.ask(RESTRICTED_Q, "Documents you can access", args.timeout)
            ref_rec = run_record(args.api, tokens[SALES], refused["run_id"])
            refused["screenshot"] = ui.shot("2-sales-refusal")
            problems = []
            if REFUSAL not in refused["text"]:
                problems.append("the reply is not the no-sources refusal")
            leaked = [m for m in RESTRICTED_MARKERS if m in page.content()]
            if leaked:
                problems.append(f"restricted content on the page: {leaked}")
            if ref_rec.get("inference_mode") != args.expect_inference:
                problems.append(f"inference_mode={ref_rec.get('inference_mode')!r}")
            result["checks"]["sales_refusal"] = {
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
    result["pass"] = (
        all(c["pass"] for c in result["checks"].values()) and len(result["checks"]) == 4
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
