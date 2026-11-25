# M6 evidence — approved email through Mailpit and scheduled briefings

Recorded 25 November 2026 on Debian 13, branch `feature/m6-wave-3-actions`. Source reports use the **fake model double** (labelled `fake` in every response and in the email body). This is evidence for the action flow, delivery semantics and authorization, not for model quality.

## Acceptance checklist

| Acceptance item | Check | Status |
| --- | --- | --- |
| Verified report → draft → approval → Mailpit | `test_m6_actions.py::test_verified_report_to_draft_to_approval_to_mailpit`; live `scripts/m6_demo.py` on the Compose stack (Celery worker, real Mailpit) → [`2026-11-25-live-demo-compose-fake-model.json`](2026-11-25-live-demo-compose-fake-model.json) | passed |
| Forged approval | `::test_forged_approval_is_rejected`; live step `send_with_forged_approval` | passed |
| Expired approval | `::test_expired_approval_is_rejected` | passed |
| Reused approval | `::test_reused_approval_is_rejected`; live step `send_reusing_approval` | passed |
| Cross-tenant / other actor / other session | `::test_approval_is_bound_to_actor_session_and_tenant`, `::test_cross_tenant_approval_token_cannot_send_own_tenant_draft` | passed |
| Edit after approval needs fresh approval | `::test_edit_after_approval_requires_fresh_approval`; live step `send_with_pre_edit_approval` | passed |
| Permission revocation before send | `::test_permission_revocation_before_send_blocks_it[sender_loses_access, recipient_loses_access, recipient_deactivated, pack_disabled]`, `::test_revocation_after_queueing_blocks_at_dispatch` | passed |
| Crash after claim, before dispatch | `::test_crash_after_claim_before_dispatch_is_reconciled_and_sent_once` (lease expiry → `uncertain` → confirmed absent → one send) | passed |
| Crash after Mailpit acceptance, before confirmation | `::test_crash_after_mailpit_acceptance_is_reconciled_without_resending` (one message, `reconciled_found`, never re-sent) | passed |
| Explicit uncertain state | `::test_ambiguous_smtp_outcome_is_uncertain_and_never_blindly_retried` (stays uncertain while Mailpit's API is down), unit SMTP phase tests | passed |
| Scheduled runs without approval | `::test_scheduled_briefing_produces_a_draft_and_never_sends_without_approval`; live beat run → [`2026-11-25-live-briefing-compose-fake-model.json`](2026-11-25-live-briefing-compose-fake-model.json) (draft stays unapproved, no delivery, after further beat passes) | passed |
| Schedules reload identity and permissions | `::test_scheduled_briefing_reloads_permissions_at_run_time`, `::test_scheduled_briefing_for_deactivated_owner_is_blocked`, `::test_scheduled_briefing_recipient_without_access_is_blocked` | passed |
| Web UI (Email and briefings card) | ESLint, `tsc`, Vite build; browser run of report → draft → edit → approve → expiry → re-approve → send → Mailpit ([`2026-11-25-browser-check-compose-fake-model.json`](2026-11-25-browser-check-compose-fake-model.json)) | passed |

## Review round 1

| Finding | Fix | Regression check | Result |
| --- | --- | --- | --- |
| 1. Access lost after the claim, before SMTP handoff | Handoff transaction re-checks everything under the tenant policy-epoch lock and holds it through SMTP | `::test_access_lost_after_claim_blocks_the_smtp_handoff[sender, recipient]` (Mailpit receives nothing), `::test_revocation_during_the_handoff_waits_until_the_outcome_is_recorded` | passed; with the handoff recheck removed both parametrized cases fail |
| 2. Receipts across restarts; uncertain when history is unavailable | Mailpit on a persistent database volume (Compose, CI). Round 1 added a receipt-history canary; round 2 replaced it (see below) | `::test_mailpit_restart_after_acceptance_keeps_the_receipt` (real `docker restart` of Mailpit plus a new app instance → `reconciled_found`, one message) | passed |
| 3. Lock order of cancel vs dispatch | All paths lock action → outbox (`store.lock_action_then_outbox`); recovery uses `SKIP LOCKED` | `::test_cancel_waits_for_the_claim_instead_of_deadlocking` (deterministic interleaving), `::test_cancel_racing_dispatch_is_consistent` (8 races) | passed; with the old outbox → action order PostgreSQL reports `deadlock detected` |
| 4. Re-approval after expiry | API already allowed it (now tested); the web card drops an expired or consumed approval and offers "Approve this version" again | `::test_same_version_can_be_approved_again_after_expiry`; browser run | passed |

## Review round 2

| Finding | Fix | Regression check | Result |
| --- | --- | --- | --- |
| A surviving canary does not prove a missing email was never accepted; deleting only that email made reconciliation re-queue it | Durable SMTP-start record per attempt (`email_smtp_attempts`, migration 0014, which also drops the canary table), committed on a separate small engine after the handoff recheck and before the first SMTP byte. No record → the attempt never reached SMTP → automatic retry (`never_reached_smtp_retry`). Record + receipt in Mailpit → `delivered`. Record + no receipt → stays `uncertain` (`receipt_missing_after_smtp`), never re-sent | `::test_receipt_deleted_after_acceptance_stays_uncertain_and_is_never_resent` (real Mailpit: accept, crash before confirmation, delete only that email while an earlier receipt stays, reconcile 3×: stays uncertain, the counting mailer sees no second SMTP call, one SMTP-start record). Still recovering automatically: `::test_crash_after_claim_before_dispatch_is_reconciled_and_sent_once` (no record → retry → one message) and `::test_crash_after_mailpit_acceptance_is_reconciled_without_resending` / `::test_mailpit_restart_after_acceptance_keeps_the_receipt` (found → delivered) | passed; with the old "absent → re-queue" rule restored the new test fails (`['requeued'] == ['receipt_missing']`) |

The first full-suite run of this round failed 2 M6 tests that passed in isolation (`test_revocation_during_the_handoff_waits_until_the_outcome_is_recorded`, `test_receipt_deleted_after_acceptance_stays_uncertain_and_is_never_resent`). Their tracebacks were not kept. Both tests were hardened: the receipt test now waits for Mailpit to index the accepted message, as the older crash test does, and the revocation test checks its precondition and asserts that the membership row was still present during SMTP. Afterwards, the M6 module passed 3 runs out of 3 and the full suite 2 out of 2. One more full run, before the hardening, also passed.

## Results

| Check | Result |
| --- | --- |
| Backend full suite (host, against Compose PostgreSQL 17.6, Redis, Qdrant, persistent Mailpit v1.27.11; Mailpit restart enabled) | After review round 2: **882 passed, 0 failed, 0 skipped**, twice (314 s, 323 s); 46 of them M6 (38 integration incl. parametrized cases, 8 unit). Round 1: 882 passed. Before review: 874 passed |
| Ruff, Ruff format, Mypy (`app`, `tests`) | clean |
| Web: ESLint, `tsc`, Vite build | clean |
| Live demo (`scripts/m6_demo.py`, Compose, Celery worker) | all 6 steps passed before and after review rounds 1 and 2 (round 2 on migration 0014; the worker wrote the SMTP-start record for attempt 1); exactly one Mailpit message for the Message-ID |
| Live briefing (Celery beat) | occurrence `review`, draft `origin=briefing`, no approval, no delivery after further beat passes |

## Reproduce

```bash
python3 scripts/dev.py test-backend       # whole backend suite in Compose, Mailpit included
python3 scripts/dev.py check              # Ruff, Mypy, unit tests, ESLint, tsc, Vite build
MODEL_MODE=fake docker compose up -d --build && python3 scripts/m6_demo.py
```

From the host against the Compose services (container IPs from `docker inspect`): set `TEST_DATABASE_ADMIN_URL`, `TEST_REDIS_URL`, `TEST_QDRANT_URL`, `TEST_MAILPIT_SMTP_HOST`, `TEST_MAILPIT_API_URL`, `TEST_MAILPIT_RESTART_CMD="docker restart eaios-mailpit-1"`, `EAIOS_REQUIRE_MAILPIT_RESTART=1`, `EAIOS_REQUIRE_INTEGRATION=1`, then `cd backend && .venv/bin/pytest -q tests/integration/test_m6_actions.py tests/unit/test_m6_mail_and_render.py`.

## Gaps

- Only verified **sales reports** can be emailed or scheduled; document, legal and code answers are not email sources yet.
- Delivery is Mailpit only. Reconciliation depends on Mailpit's search API; a real SMTP relay has no equivalent, so this is not an end-to-end exactly-once guarantee (ADR 0601). An attempt that may have reached SMTP and whose message is not in Mailpit (deleted, pruned at `MP_MAX_MESSAGES` 5000, or a crash between the SMTP-start record and the first SMTP byte) stays uncertain until an operator resolves it; there is no operator endpoint for that yet.
- The approver must be the draft owner; there is no separate approver role or two-person approval.
- Live runs used the fake model; no real-model briefing was run.

The Mailpit restart test needs `TEST_MAILPIT_RESTART_CMD`; CI sets it to restart its Mailpit service container. Inside `python3 scripts/dev.py test-backend` (no Docker access) it is skipped.
