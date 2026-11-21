# Requirement → test traceability (M0–M2)

M0 implements FR-01 and the foundations of FR-02 and FR-11, plus NFR-01, NFR-03 and NFR-05. Document, search and SQL behaviour (most of T1–T10) belongs to later milestones and is **not** marked complete here.

| Requirement | What M0 delivers | Evidence |
| --- | --- | --- |
| FR-01 Identity | Login, Argon2id, JWT validation, per-request server-built context, logout, deactivation, rate limit | `tests/integration/test_auth_api.py`, `tests/unit/test_tokens_and_redaction.py`, `scripts/smoke_stack.py` |
| FR-02 Filtered retrieval (foundation) | Reference five-layer evaluator, declarative fixtures, DB-backed decisions; retrieval design only | `tests/unit/test_policy_evaluator.py`, `tests/unit/test_policy_fixtures.py`, `tests/integration/test_authorization_db.py`, ADR 0103/0104 |
| FR-11 Audit (foundation) | `audit_events` for login allow/deny/rate-limit, token rejection, logout and authorization decisions; app role insert-only | `test_auth_api.py::test_audit_events_and_no_secret_leakage`, `test_authorization_db.py::test_authorization_decisions_are_audited`, `test_tenant_isolation.py::test_audit_events_not_readable_by_app_role` |
| NFR-01 Isolation (foundation) | RLS on every tenant table for a non-owner role, composite cross-tenant FKs, transaction-scoped context, pool cleanup | `tests/integration/test_tenant_isolation.py` |
| NFR-03 Compose | One `docker compose up --build`, health-gated, named volumes, restart persistence | `compose.yaml`, runbook, PR acceptance log |
| NFR-05 Secrets | Secrets only in `.env`/env, `SecretStr`, log redaction, no tokens in browser storage, no secrets in logs or responses | `test_auth_api.py::test_audit_events_and_no_secret_leakage`, `test_health.py`, `test_tokens_and_redaction.py`, ESLint storage rule |

## M1 data fixtures

| Requirement | What M1 delivers | Evidence |
| --- | --- | --- |
| NFR-06 Dataset (foundation) | Verified NileTech 0.1.6 sales baseline (manifest, hashes, provenance pinned), deterministic normalization and fingerprint | `tests/unit/test_business_source.py`, `tests/integration/test_business_import.py::test_two_clean_database_generations_are_identical` |
| FR-07 / FR-14 (inputs for M2) | Explicit repeatable import; reconciled lines, orders and invoices; frozen Q1 2026 evaluation case with a hand-checked subset | `tests/unit/test_sales_reference.py`, `tests/integration/test_business_import.py`, `docs/data/sales-report-q1-2026.md` |
| NFR-01 Isolation | RLS and read-only grants on sales tables; isolation tenant sees no business rows | `test_business_import.py::test_isolation_tenant_and_missing_context_see_no_business_rows` |

## M1 parser comparison

| Requirement | What M1 delivers | Evidence |
| --- | --- | --- |
| FR-05 Deterministic ingestion (input) / ADR 0016 | Parser benchmark on digital, two-column, table-heavy and scanned PDFs plus CSV and workbook cases; seven synthetic fixtures with SHA-256 provenance and hand-checked gold; laptop results and Colab results for the model-based candidates (explicit run selection in `results/selection.json`); recommendation: Docling for complex PDFs, Python `csv`, openpyxl | `ml/parser-benchmark/reports/2026-11-20-parser-comparison.md`, `ml/parser-benchmark/tests/`, CI job `parser-benchmark` |

## M2 verified SQL reports

Tests below are in `backend/tests/`. Integration tests run on real PostgreSQL 17.6 with deterministic model doubles (`FakeModelClient`, labelled fake); they are integration evidence, not model-quality evidence. The real-model demonstration is listed separately.

| Requirement | What M2 delivers | Evidence |
| --- | --- | --- |
| FR-07 Read-only SQL | Separate `eaios_report` role (non-owner, no superuser/BYPASSRLS), `reporting` security-invoker views, restrictive RLS bound to an application-minted backend context, parsed allowlist, read-only transaction, timeouts, row limit | `integration/test_report_sql_security.py`, `unit/test_report_sql_guard.py`, ADR 0201 |
| FR-07 / FR-14 Report semantics | Rule-derived intent from tenant profile; AST checks of date column/window, statuses, measures, grain, filters; join-multiplicity probe; exact decimal result checks; frozen Q1 2026 plus three independently calculated periods | `unit/test_report_semantics.py`, `unit/test_sales_generalization_reference.py`, `integration/test_report_workflow.py::test_frozen_q1_report_is_published_with_evidence`, `::test_other_periods_use_runtime_verification_not_stored_answers`, ADR 0204 |
| FR-06 Agent routing | LangGraph graph prepare → route → SQL → check → execute → evidence → draft → verify → publish/finish; rules first, model router only for unrecognised topics | `app/reporting/workflow.py`, `test_report_workflow.py::test_unsupported_or_ambiguous_requests_abstain`, `::test_unavailable_inference_fails_without_partial_output[router]` |
| FR-09 Verification | Deterministic claim checks plus development NLI baseline; ≤ 2 repairs; failed drafts never stored or streamed; structured-only publication when narrative cannot be verified | `test_report_workflow.py::test_bad_sql_is_repaired_and_never_shown`, `::test_exhausted_sql_repairs_abstain_without_figures`, `::test_bad_narrative_is_repaired_and_hidden`, `::test_exhausted_narrative_repairs_publish_structured_only`, `::test_checker_rejection_hides_unsupported_narrative`, `::test_checker_failure_publishes_only_deterministic_results` |
| FR-03 Progress and citations (SQL part) | Six SSE events, persisted with increasing ids; only `status` before publication; table cells and citations reference immutable evidence by row/column; evidence view with hash integrity | `report_support.assert_only_status_before_publication`, `test_report_workflow.py::test_reconnect_replays_identical_events`, `apps/web/src/ReportPanel.tsx` |
| FR-02/NFR-01 Authorization and isolation | `dataset.query` through the five-layer policy; denied same-tenant users and the isolation tenant get one indistinguishable abstention; revocation at four run stages and before publication; revoked replay and evidence; foreign ids 404 | `fixtures/policy_cases.toml`, `test_report_workflow.py::test_denied_and_isolated_users_get_the_same_abstention`, `::test_revocation_during_a_run_prevents_publication`, `::test_replay_and_evidence_after_revocation_expose_nothing`, `::test_foreign_and_unknown_ids_are_not_found`, ADR 0202/0203 |
| FR-11 Audit | `report.run/authorize/query/publish/finish/replay/evidence` events with decisions and private reasons, no figures or SQL text | `test_report_workflow.py::test_audit_records_report_decisions_without_figures` |
| NFR-02 Responsiveness | Progress streams immediately; answer only after verification; stage timings recorded per run (`diagnostics.timings`) | `scripts/m2_demo_capture.py` |
| NFR-04 Model endpoint | One root, explicit chat/embed/check/health paths, bearer service auth, timeouts, cancellation, typed failures, pinned revisions; unavailable gateway fails clearly | `unit/test_model_client.py`, `unit/test_model_gateway_contract.py`, `test_report_workflow.py::test_unconfigured_gateway_reports_model_unavailable` |
| Jobs and durability | Run + job row in one transaction; Celery dispatch; beat re-dispatch; claim/lease; unique terminal event | `test_report_workflow.py::test_idempotent_create_and_duplicate_job_delivery`, `::test_outbox_redispatches_a_lost_job` |
| Review fixes (PR #5) | Value-changing casts rejected (AST and runtime scale); per-event SSE reauthorization; query context bound to session identity + transaction secret (migration 0004) | `unit/test_report_semantics.py::test_value_changing_casts_are_rejected`, `test_report_workflow.py::test_rounded_amounts_never_receive_a_verified_label`, `::test_runtime_scale_check_blocks_rounding_the_ast_missed`, `::test_revocation_between_frames_of_one_batch_stops_content`, `test_report_sql_security.py::test_stale_context_is_not_inherited_by_a_reused_pid`, `::test_crashed_backend_context_is_cleaned_up`, `::test_pooled_connection_with_lingering_row_sees_nothing` |
| **Real-model demonstration** | Gateway and capture script are implemented | **Not yet run** — requires the owner's Colab T4 gateway; see the PR's unresolved items |

## Security tests T1–T10 status

| Test | Status (M0, updated for M2) |
| --- | --- |
| T1 / T2 | Policy-level only. Fixture cases show a finance reader allowed and a sales reader denied the finance report (in memory and from PostgreSQL). Retrieval-level tests come in M3. |
| T3 | Database level: the app role cannot read another tenant's rows, and a cross-tenant resource decision is denied. Retrieval level comes in M3. |
| T4 | Internal group removal changes the next decision (`test_group_removal_changes_next_decision`). Sync-based removal comes in M4. |
| T5 | Not applicable yet: there is no file or document endpoint. M3. |
| T6 | **M2:** generated SQL cannot bypass database restrictions: validator rejections plus raw query-role attempts that bypass the validator (forged context, public objects, writes, DDL, borrowed context), cross-tenant predicates and joins, timeouts, row limits, pool reuse (`test_report_sql_security.py`). |
| T7 | Evaluator level: unknown inputs deny (`test_unknown_required_inputs_deny`). Ingestion level comes in M3. |
| T8 | Not applicable: no cache. Later. |
| T9 | Not applicable: no Qdrant queries. Design in ADR 0104. M3. |
| T10 | Not applicable. M3. |
