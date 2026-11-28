# ADR 0801 — Automatic chat routing and private, persistent chat history

Status: Proposed for M8 (pending planning review). Relates to: the M8 chat (PR #21, #22), ADR 0101 (session token in memory), ADR 0303/0304 (retrieval filtered by current permissions, citations re-checked), ADR 0601 (email only from a verified report after human approval). Implementation: `backend/app/chat/routing.py`, `backend/app/chat/history.py`, `backend/app/api/conversations.py`, migration 0017; `apps/web/src/ChatPage.tsx`, `chatState.ts`, `history.ts`.

## Decision

1. **The server routes; the employee does not pick a source.** For `POST /api/v1/chat` with the default `mode = auto`, `route_chat` chooses exactly one existing handler (`general`, `documents`, `report`, `legal`, `code`) among the tenant's enabled packs, or `clarify`. The rules are fixed and testable; neither the client nor model output can choose or widen the handler. A leading greeting is stripped first, so it never hides a company request; a greeting alone, definitions that do not name the company's own ("What is a contract?") and calendar questions (a period without business-figure words) go to General; company terms go to their handler; a company question whose handler is disabled falls back to documents or the sales report, never to General. Explicit modes stay for API clients, the Approvals report form and tests; briefings keep the earlier router and never use General.
2. **Handlers are unchanged.** Routing only selects. Each handler applies its own permission checks, verification and citations, so a question routed to documents the employee cannot open ends with the documents handler's refusal, exactly as before.
3. **Clarify is not a run.** A request too vague to route gets one short question naming only the enabled handlers, streamed as two events in the run format (`status`, `done` with `abstention.code = clarification_needed`). No run row, no model call.
4. **The handler is visible.** The run's first event carries `handler`, `handler_label` and, for routed runs, `route_reason` (also audited), so the label survives replay; `X-Handler` names it in the response.
5. **History is server-side and owner-only.** `chat_conversations` and `chat_turns` store the question, the handler and the answering run id (or the clarifying text). Answers are not copied: every reopen (not only the first) hides the saved answers in the browser at once and replays each run through `GET /runs/{id}/events`, which re-authorizes content, so revoked content never reappears from the page's memory; citations are re-checked when opened. Every query filters on `owner_user_id`, and a RESTRICTIVE `owner_only` row policy (on `app.user_id`) enforces it in the database. Unknown, other-employee and other-organization ids all return the same 404, including `conversation_id` on `POST /chat`, which then creates no run.
6. **Delete removes history, not runs.** Deleting a conversation (audited, `chat.conversation.delete`) cascades to its turns; runs stay because approvals and the audit log refer to them, and they remain owner-only.
7. **The token stays in memory (ADR 0101).** The browser stores nothing; after a reload the employee signs in again and Recents is fetched from the server.
8. **No conversational context yet.** Earlier turns are still not sent to the model; follow-up reasoning is a separate change.

## Consequences

- Rules can misroute unusual phrasings; the label shows which handler answered, and a misrouted company question is still bounded by that handler's permission checks. A model-assisted router can be added later behind the same `route_chat` contract.
- History rows are small (question text, ids). Run retention is unchanged.

## Evidence

`backend/tests/unit/test_chat_routing.py`; `backend/tests/integration/test_m8_chat_history.py` (routing end to end, persistence across sign-ins, idempotent retries, other-employee and other-organization 404s with no run created, delete and audit, the database owner policy, access revoked on replay); `scripts/m8_chat_ui_check.py` ([evidence](../evidence/m8-chat-routing/README.md)).
