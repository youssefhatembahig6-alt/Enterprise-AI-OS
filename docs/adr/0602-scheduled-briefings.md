# ADR 0602 — Scheduled briefings produce drafts for review

Status: Accepted for M6 (pending planning review). Relates to: FR-08, FR-06, roadmap "Scheduled monitoring writes permission-scoped briefings; execution reloads current identities and permissions instead of reusing a stale saved access context." Implementation: `backend/app/actions/briefings.py`, migration 0012.

## Decision

1. **A schedule stores no access context.** `briefing_schedules` holds the owner's user id, the question, the recipient's user id, the interval (1 hour to 31 days) and the next due time. No token, session, role, group or policy snapshot is saved.
2. **Questions.** Only questions the router sends to the verified sales report (`report`) are accepted in this version (`422 unsupported_question` otherwise), because only those results can become email drafts (ADR 0601).
3. **Each occurrence reloads the owner.** When a schedule is due, the beat task locks it, inserts one occurrence per due time (`UNIQUE (tenant_id, schedule_id, due_at)`), advances `next_run_at` to the first future slot (missed slots are skipped, not replayed), and reloads the owner from PostgreSQL: user and tenant active, `actions` and `sales_reports` packs enabled. The current policy epoch is recorded on the occurrence. A failure marks it `blocked` (`owner_inactive`, `pack_disabled`, `report_pack_disabled`).
4. **The run is an ordinary run.** It is created as the owner with a deterministic `client_request_id` (`briefing-<occurrence>`), so a crash between steps re-creates the same run. The report workflow authorizes `dataset.query` from current state at every stage and at publication, exactly as an interactive run. A run that is not published (denied, abstained, failed) ends the occurrence as `failed`, without content.
5. **Review only.** A published run becomes a draft (`origin = briefing`) through the same code path as an interactive draft, including the recipient's authorization check (`blocked: recipient_not_authorized` otherwise). The occurrence ends in `review` (`awaiting_human_approval`). The scheduler never creates approvals or outbox rows; sending requires the owner to approve and send in a session (ADR 0601).

## Evidence

`test_m6_actions.py::test_scheduled_briefing_produces_a_draft_and_never_sends_without_approval`, `::test_scheduled_briefing_reloads_permissions_at_run_time`, `::test_scheduled_briefing_for_deactivated_owner_is_blocked`, `::test_scheduled_briefing_recipient_without_access_is_blocked`, `::test_briefings_accept_report_questions_only`.
