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
| Web UI (Email and briefings card) | ESLint, `tsc`, Vite build | passed; **not exercised in a browser** (see gaps) |

## Results

| Check | Result |
| --- | --- |
| Backend full suite (host, against Compose PostgreSQL 17.6, Redis, Qdrant, Mailpit v1.27.11) | **874 passed, 0 failed** in 290 s; 38 of them M6 (30 integration incl. parametrized cases and the 0012 up/down round trip, 8 unit) |
| Ruff, Ruff format, Mypy (`app`, `tests`) | clean |
| Web: ESLint, `tsc`, Vite build | clean |
| Live demo (`scripts/m6_demo.py`, Compose, Celery worker) | all 6 steps passed; exactly one Mailpit message for the Message-ID |
| Live briefing (Celery beat) | occurrence `review`, draft `origin=briefing`, no approval, no delivery after further beat passes |

## Reproduce

```bash
python3 scripts/dev.py test-backend       # whole backend suite in Compose, Mailpit included
python3 scripts/dev.py check              # Ruff, Mypy, unit tests, ESLint, tsc, Vite build
MODEL_MODE=fake docker compose up -d --build && python3 scripts/m6_demo.py
```

From the host against the Compose services (container IPs from `docker inspect`): set `TEST_DATABASE_ADMIN_URL`, `TEST_REDIS_URL`, `TEST_QDRANT_URL`, `TEST_MAILPIT_SMTP_HOST`, `TEST_MAILPIT_API_URL`, `EAIOS_REQUIRE_INTEGRATION=1`, then `cd backend && .venv/bin/pytest -q tests/integration/test_m6_actions.py tests/unit/test_m6_mail_and_render.py`.

## Gaps

- The web card was built, linted and type-checked but not clicked through in a browser in this session.
- Only verified **sales reports** can be emailed or scheduled; document, legal and code answers are not email sources yet.
- Delivery is Mailpit only. Reconciliation depends on Mailpit's search API; a real SMTP relay has no equivalent, so this is not an end-to-end exactly-once guarantee (ADR 0601). A worker that stalls longer than the 120 s lease and then finishes its SMTP exchange after recovery re-queued the row could still produce a second copy.
- The approver must be the draft owner; there is no separate approver role or two-person approval.
- Live runs used the fake model; no real-model briefing was run.
