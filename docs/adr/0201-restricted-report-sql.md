# ADR 0201 — Restricted report SQL execution

Status: accepted for M2, pending planning-agent review. Source: specification §8 "Safe text-to-SQL" and "PostgreSQL" connector rules, FR-07, T6; roadmap "M2 SQL and reporting details". Implementation: `backend/app/db/migrations/versions/0003_verified_reports.py`, `backend/app/reporting/sql_guard.py`, `backend/app/reporting/executor.py`, `infra/postgres/report-role.sql`.

## Decision

Generated SQL is untrusted input and runs through layers that each hold on their own:

1. **Separate query role** `eaios_report`: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8`, owns nothing (the migration refuses otherwise). Created by a superuser (init script, `scripts/dev.py add-report-role`, test harness) because the owner role cannot create roles. Role defaults: `default_transaction_read_only=on`, `statement_timeout=5000`, `lock_timeout=1000`, `idle_in_transaction_session_timeout=15000`, `search_path=reporting`, `work_mem=16MB`, `temp_file_limit=32MB`.
2. **Restricted surface**: `USAGE` only on schema `reporting` (five `security_invoker, security_barrier` views without `tenant_id`) and `reporting_ctx`, plus column-level `SELECT` on exactly the exposed base columns. `USAGE` on `public` is revoked from `PUBLIC` and granted to the application role only, so the query role cannot name application tables or functions.
3. **Unforgeable, session-bound tenant context** (revised by migration `0004_report_session_identity` after review). Each sales table has a permissive *and a restrictive* policy for the query role: `tenant_id = (SELECT reporting_ctx.report_context_tenant())`, evaluated once per query. A context row is keyed to a **database session identity**, `(backend_pid, backend_start)`, plus the SHA-256 of a per-binding **secret**:
   - The query connection reads its own `backend_start` (PostgreSQL shows it to the session itself and hides it from other roles). A later backend that reuses the PID has a different start time, so it cannot inherit a stale row.
   - The application generates a 256-bit secret, stores only its digest through `report_context_open(pid, backend_start, secret_sha256, run, ttl)` (executable only by the application role, which takes tenant and user from its own trusted transaction and checks the PID is a query-role backend), and the executor sets the secret with `SET LOCAL eaios.report_ctx` inside the query transaction. It exists nowhere else and disappears at COMMIT, ROLLBACK or `DISCARD ALL`.
   - In the RLS path, the SECURITY INVOKER wrapper `reporting_ctx.report_context_tenant()` passes the caller's own session start and transaction secret to the SECURITY DEFINER lookup `reporting_ctx.report_context_lookup()`, which also requires `pg_backend_pid()`, `session_user` and an unexpired row. Calling the lookup directly with a claimed start time still needs the secret.
   - Opening any context first deletes expired rows and rows of backends that no longer exist (crash cleanup). Forging `app.tenant_id` in the query session changes nothing.
4. **Parsed allowlist** (`validate_sql`): one plain `SELECT` from allowlisted node types, relations and functions; the executor runs the deparsed validated tree. A textual `SELECT` prefix is never relied on, and no LLM judges safety.
5. **Per-transaction limits**: `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout` (5 s default), lock/idle timeouts, `search_path`, `work_mem`; outer `LIMIT max_rows + 1` with a hard `too_many_rows` failure; only scalar result types.
6. **Pool hygiene**: the context row is always closed in `finally`; checked-in connections are rolled back and cleaned with `DISCARD ALL`; prepared statements are disabled for pooled connections.

## Evidence

`tests/integration/test_report_sql_security.py` asserts session binding (`test_context_requires_session_identity_and_transaction_secret`, `test_stale_context_is_not_inherited_by_a_reused_pid`, `test_crashed_backend_context_is_cleaned_up`, `test_pooled_connection_with_lingering_row_sees_nothing`), role properties and privileges from the catalog; runs raw SQL as the query role *without* the validator (no context → 0 rows; forged `app.tenant_id` → 0 rows; `public` objects, writes, DDL, temp tables, `SET ROLE` and `report_context_open` → errors; another backend's context cannot be borrowed); exact Q1 figures through the executor; isolation tenant sees no rows; cross-tenant id/value/join predicates return nothing while the isolation tenant sees only its own marker row; statement timeout; row limit; pool reuse across tenants on one physical connection with no leftover context; context closed after a failed query. Validator cases: `tests/unit/test_report_sql_guard.py`.

## Limits

- *Resolved (review finding 3):* the original PID-only binding could be inherited by a new backend that reused a crashed worker's PID within the TTL. Contexts now also require the session's `backend_start` and the transaction-local secret, and dead backends' rows are purged on every open.
- An attacker with raw query-role credentials could still `SET statement_timeout = 0` in their own session; the role default and executor `SET LOCAL` bound the application path. Data access is bounded by RLS regardless.
- PostgreSQL built-ins executable by `PUBLIC` (for example `pg_sleep`) remain executable by the role at the database level; the validator rejects them and the timeouts bound them.
