# M8 automatic chat routing and private chat history: evidence

Branch `feature/m8-chat-routing-history`, stacked on PR #22 (`feature/m8-real-model-chat`), whose General mode it routes to. Synthetic NileTech fixtures only.

| Run | Inference | Result |
| --- | --- | --- |
| Browser check, answering checks: automated-test UI build (`VITE_TEST_ALLOW_FAKE_MODEL=true`) on :13101 → disposable API :18000, 28 Nov 2026 14:11 UTC | **Fake** (labelled test double; UI and routing evidence only, never model evidence) | **PASS**, all 10 checks ([JSON](2026-11-28-local-routing-history-fake-model.json)) |
| Real-model-only gate, **normal** UI (:13000) against the fake-mode backend, 14:10 UTC | Fake backend = **Real model offline** | **PASS**, 3/3: badge **Real model offline**, Send disabled and nothing sent on Enter, saved chat opens ([JSON](2026-11-28-local-offline-gate-fake-mode.json)) |
| Real-model-only gate, normal UI against a **gateway-mode** API whose gateway is unreachable (`error: unavailable`), 14:12 UTC | Gateway unavailable | **PASS**, 3/3 ([JSON](2026-11-28-local-offline-gate-gateway-unavailable.json)) |
| Backend integration tests (`tests/integration/test_m8_chat_history.py`), real PostgreSQL 17.6 + Redis | Fake | **10 passed** |
| Routing unit tests (`tests/unit/test_chat_routing.py`) | none (rules only) | **71 passed** |
| Web tests: reopening hides saved answers every time, also during a live reply; real-model-only `canSend` (`apps/web/test/chat.test.ts`, `modelStatus.test.ts`) | none | **pass** (web 47/47) |
| Real Qwen (Colab T4, notebook cells 1–5 or 1–3 + 7 on this branch) | **Real Qwen3-8B-AWQ** | **Not run yet:** needs the owner's Colab run |

## Browser check (fake model)

`scripts/m8_chat_ui_check.py --expect-inference fake`. There is no source picker; every question was typed and sent, and the server routed it.

| Check | What happened |
| --- | --- |
| status_badge | **FAKE model (test build)** (only in the automated-test build; normal builds show **Real model offline** for a fake backend and **Connected · Qwen3-8B-AWQ** on the gateway) |
| no_source_picker | The composer has no Source control |
| hello | Routed to **General** (`route_reason: greeting`); reply published, no citations |
| general | *"Explain in plain words what a confusion matrix is."*: **General**, uncited, saved to the same chat |
| documents_leave | *"How many unused leave days can I carry into next year, and when do they expire?"*: **Documents**, cited; the citation opened the passage (`GET …/citations/c1` 200, *text hash verified*) |
| sales_report | *"What were fulfilled sales in Q1 2026?"*: **Sales report (SQL)**, verified table (291 orders, net USD 80,934,385.00), **Numbers match query rows**; **Email this report for approval** opened the approval form for *Fulfilled sales, Q1 2026* (nothing drafted or sent) |
| clarify | *"the numbers?"*: **Clarifying question** — *"Could you say a little more? Is this about a company policy or document, sales figures for a period, a contract, company code or a general question?"*; no run started |
| revalidation | Same session, no reload. A newer question ("hello") was sent in the same chat and held in flight before the switch; during the re-check the older answer was hidden while the newer reply still showed *Working…*, and after the older answer's replay ended with `access_revoked` the newer reply finished normally (**General**). finance.reader first asked *"Why did collections lag revenue in Q1 2026?"* (**Documents**, cited from the finance commentary) and opened the citation; switched to a new chat; access.admin removed finance.reader from `finance-team`; the chat was reopened from Recents with its replay held open. **During** the re-check: only the question, the **Documents** label and *Re-checking your access…*; no text, no citation, no passage, old text absent from the page. **After**: *"You no longer have access to this answer, so it is hidden. (access_revoked)"*, no citation, no restricted marker; the citation endpoint returned **404**. Membership was restored (in a `finally`) and the next reopen showed the answer again |
| history | After a reload and a new sign-in, all three chats were under **Recents**; the first reopened with both answers replayed and labelled **General**; sales.reader got `listed: false`, open **404**, delete **404**, and saw none of finance.reader's chats; **Delete** with confirmation removed it from Recents and the API, and it stayed gone after another reload |
| sales_refusal | sales.reader: *"Why did collections lag revenue in Q1 2026?"* routed to **Documents**, the no-sources refusal, no restricted figures on the page |

## What the integration tests add (real PostgreSQL)

- "hello" → General; a sales question → the verified SQL report with SQL-evidence citations; a vague request → a clarifying question with **no run row**; an explicit API `mode` still runs that handler.
- Conversations persist across sign-ins, list most-recent first, and reopen through the run (the first replayed event carries the handler label).
- Retrying with the same `client_request_id` adds no second turn.
- Another employee in the same organization and an employee of another organization both get 404 on open, append (`conversation_id` on `POST /chat`, which also creates **no run**) and delete; unknown ids look the same.
- The owner's delete returns 204, removes the turns, is audited (`chat.conversation.delete`), and the chat stays gone (404 on open, delete and append).
- The database's RESTRICTIVE `owner_only` policy: a query **without** an owner filter, in another employee's session, sees and deletes nothing, and planting a row for another owner is refused by row-level security.
- After the employee loses access (finance team membership removed), the saved chat still lists the run, but replaying it ends with `access_revoked` and no content, and its SQL evidence returns 404.

## Routing revision (PR #23 review)

- A greeting in front of a request keeps the request's intent: *"Hi, what is our leave policy?"* → Documents, *"Good morning, what were fulfilled sales in Q1 2026?"* → Sales report, *"hello, when do our contracts expire?"* → Contracts. A greeting alone ("hello", "thanks, that helped") → General.
- Ordinary questions → General: *"What is a contract?"*, *"What's an NDA?"*, *"Define a termination clause."* (definitions that do not name the company's own), *"How many days are in February 2026?"*, *"What day of the week is 1 March 2026?"* (a period without business-figure words).
- Unchanged: *"What were fulfilled sales in Q1 2026?"* → Sales report (`sales_terms`); *"What is our contract with Red Sea Logistics?"* → Contracts.

## Revision: record requests, reopen during a live reply, real-model only

- *"Help me find our board minutes"* → Documents (small talk now matches only a whole message, so "help" alone is still General); *"What is a customer in the board memo?"* → Documents (a definition that points at a specific company record is about that record). *"What is a customer?"*, *"What is a memo?"*, "hello" stay General.
- Reopening a chat hides and re-checks every completed answer even while a newer reply is streaming; the live reply is not replaced or replayed and finishes normally.
- The employee-facing chat is real-model only: Send is enabled only while the backend has verified the pinned Qwen (authenticated readiness); a fake-mode backend or an unavailable gateway shows **Real model offline**. The fake model stays a test double behind `VITE_TEST_ALLOW_FAKE_MODEL=true`.
- **Real Qwen (standalone Colab chat):** the runner now also runs the offline gate against a second API with an unreachable gateway. **Not run yet:** needs the owner's Colab run (cells 1–3 and 7 on this branch).
