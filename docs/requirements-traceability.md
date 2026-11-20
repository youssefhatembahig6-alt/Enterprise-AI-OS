# Requirement → test traceability (M0 scope only)

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

## Security tests T1–T10 status

| Test | Status in M0 |
| --- | --- |
| T1 / T2 | Policy-level only. Fixture cases show a finance reader allowed and a sales reader denied the finance report (in memory and from PostgreSQL). Retrieval-level tests come in M3. |
| T3 | Database level: the app role cannot read another tenant's rows, and a cross-tenant resource decision is denied. Retrieval level comes in M3. |
| T4 | Internal group removal changes the next decision (`test_group_removal_changes_next_decision`). Sync-based removal comes in M4. |
| T5 | Not applicable yet: there is no file or document endpoint. M3. |
| T6 | Not applicable: no generated SQL. M2. |
| T7 | Evaluator level: unknown inputs deny (`test_unknown_required_inputs_deny`). Ingestion level comes in M3. |
| T8 | Not applicable: no cache. Later. |
| T9 | Not applicable: no Qdrant queries. Design in ADR 0104. M3. |
| T10 | Not applicable. M3. |
