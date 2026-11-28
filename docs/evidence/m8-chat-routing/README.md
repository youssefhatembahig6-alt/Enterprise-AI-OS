# M8 automatic chat routing and private chat history: evidence

Branch `feature/m8-chat-routing-history`, stacked on PR #22 (`feature/m8-real-model-chat`), whose General mode it routes to. Synthetic NileTech fixtures only.

| Run | Inference | Result |
| --- | --- | --- |
| Browser check, disposable stack `eaios-m7acc` (web :13000, API :18000), 28 Nov 2026 13:16 UTC | **Fake** (labelled development double; UI and routing evidence only, never model evidence) | **PASS**, all 9 checks ([JSON](2026-11-28-local-routing-history-fake-model.json)) |
| Backend integration tests (`tests/integration/test_m8_chat_history.py`), real PostgreSQL 17.6 + Redis | Fake | **10 passed** |
| Routing unit tests (`tests/unit/test_chat_routing.py`) | none (rules only) | **36 passed** |
| Real Qwen (Colab T4, notebook cells 1–5 or 1–3 + 7 on this branch) | **Real Qwen3-8B-AWQ** | **Not run yet:** needs the owner's Colab run |

## Browser check (fake model)

`scripts/m8_chat_ui_check.py --expect-inference fake`. There is no source picker; every question was typed and sent, and the server routed it.

| Check | What happened |
| --- | --- |
| status_badge | **FAKE model** (correct for fake mode; **Connected · Qwen3-8B-AWQ** on the gateway) |
| no_source_picker | The composer has no Source control |
| hello | Routed to **General** (`route_reason: greeting`); reply published, no citations |
| general | *"Suggest a simple agenda for a 30-minute weekly team retrospective."*: **General**, uncited, saved to the same chat |
| documents_leave | *"How many unused leave days can I carry into next year, and when do they expire?"*: **Documents**, cited; the citation opened the passage (`GET …/citations/c1` 200, *text hash verified*) |
| sales_report | *"What were fulfilled sales in Q1 2026?"*: **Sales report (SQL)**, verified table (291 orders, net USD 80,934,385.00), **Numbers match query rows**; **Email this report for approval** opened the approval form for *Fulfilled sales, Q1 2026* (nothing drafted or sent) |
| clarify | *"the numbers?"*: **Clarifying question** — *"Could you say a little more? Is this about a company policy or document, sales figures for a period, a contract, company code or a general question?"*; no run started |
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
