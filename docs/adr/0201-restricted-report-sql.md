# ADR 0201 — Restricted report SQL execution

Status: accepted for M2, pending planning-agent review. Source: specification §8 "Safe text-to-SQL" and "PostgreSQL" connector rules, FR-07, T6; roadmap "M2 SQL and reporting details". Implementation: `backend/app/db/migrations/versions/0003_verified_reports.py`, `backend/app/reporting/sql_guard.py`, `backend/app/reporting/executor.py`, `infra/postgres/report-role.sql`.

## Decision

Generated SQL is untrusted input and runs through layers that each hold on their own:

1. **Separate query role** `eaios_report`: `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8`, owns nothing (the migration refuses otherwise). Created by a superuser (init script, `scripts/dev.py add-report-role`, test harness) because the owner role cannot create roles. Role defaults: `default_transaction_read_only=on`, `statement_timeout=5000`, `lock_timeout=1000`, `idle_in_transaction_session_timeout=15000`, `search_path=reporting`, `work_mem=16MB`, `temp_file_limit=32MB`.
2. **Restricted surface**: `USAGE` only on schema `reporting` (five `security_invoker, security_barrier` views without `tenant_id`) and `reporting_ctx`, plus column-level `SELECT` on exactly the exposed base columns. `USAGE` on `public` is revoked from `PUBLIC` and granted to the application role only, so the query role cannot name application tables or functions.
3. **Unforgeable tenant context**. Each sales table has a permissive *and a restrictive* policy for the query role: `tenant_id = reporting_ctx.report_context_tenant()`. That function returns the tenant of the context row for `pg_backend_pid()`, only when `session_user` is the query role and the row has not expired. Rows are written only through `report_context_open(pid, run, ttl)`, executable only by the application role, which takes the tenant and user from its own trusted transaction (`app.tenant_id`/`app.user_id`, set from the verified session) and checks that the PID is a query-role backend. Forging `app.tenant_id` in the query session changes nothing, because the restrictive policy still requires the bound context.
4. **Parsed allowlist** (`validate_sql`): one plain `SELECT` from allowlisted node types, relations and functions; the executor runs the deparsed validated tree. A textual `SELECT` prefix is never relied on, and no LLM judges safety.
5. **Per-transaction limits**: `SET TRANSACTION READ ONLY`, `SET LOCAL statement_timeout` (5 s default), lock/idle timeouts, `search_path`, `work_mem`; outer `LIMIT max_rows + 1` with a hard `too_many_rows` failure; only scalar result types.
6. **Pool hygiene**: the context row is always closed in `finally`; checked-in connections are rolled back and cleaned with `DISCARD ALL`; prepared statements are disabled for pooled connections.

## Evidence

`tests/integration/test_report_sql_security.py` asserts role properties and privileges from the catalog; runs raw SQL as the query role *without* the validator (no context → 0 rows; forged `app.tenant_id` → 0 rows; `public` objects, writes, DDL, temp tables, `SET ROLE` and `report_context_open` → errors; another backend's context cannot be borrowed); exact Q1 figures through the executor; isolation tenant sees no rows; cross-tenant id/value/join predicates return nothing while the isolation tenant sees only its own marker row; statement timeout; row limit; pool reuse across tenants on one physical connection with no leftover context; context closed after a failed query. Validator cases: `tests/unit/test_report_sql_guard.py`.

## Limits

- A context row is bound to a backend PID for at most its TTL (30 s by default). If a worker crashed after opening a context and the operating system reused that PID for another query-role connection within the TTL, that connection would see the same tenant. Exposure is bounded by the TTL and to the same role; the executor replaces any row for its PID before use.
- An attacker with raw query-role credentials could still `SET statement_timeout = 0` in their own session; the role default and executor `SET LOCAL` bound the application path. Data access is bounded by RLS regardless.
- PostgreSQL built-ins executable by `PUBLIC` (for example `pg_sleep`) remain executable by the role at the database level; the validator rejects them and the timeouts bound them.
