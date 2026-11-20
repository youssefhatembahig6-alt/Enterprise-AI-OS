# ADR 0102 — Tenant boundary in PostgreSQL

Status: accepted for M0, pending planning-agent review. Source: [specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f) §8 "Multi-tenant isolation" and "Safe text-to-SQL", ADR 0003 (Decided), NFR-01.

## Decision

**Roles** (created by `infra/postgres/init-roles.sh` on first start; random names in tests):

| Role | Attributes | Used by |
| --- | --- | --- |
| `postgres` (superuser) | bootstrap only | postgres container init, optional test profile |
| `eaios_owner` | LOGIN, NOSUPERUSER, NOBYPASSRLS; owns schema objects | `migrate`, `seed-dev` |
| `eaios_app` | LOGIN, NOSUPERUSER, NOBYPASSRLS, NOINHERIT; owns nothing | api, worker, scheduler |

Row-Level Security does not apply to table owners or BYPASSRLS roles, so isolation is demonstrated only with `eaios_app`. RLS is *enabled* but not *forced*: the owner role is the privileged migration role and deliberately bypasses it.

**Tenant-owned tables** (each has `tenant_id NOT NULL`, RLS enabled and policy `tenant_isolation` with `USING` and `WITH CHECK (tenant_id = app_current_tenant_id())`): `users`, `roles`, `role_capabilities`, `user_roles`, `groups`, `group_memberships`, `resources`, `resource_grants`, `sessions`.

Special cases: `tenants` — RLS, the app may SELECT only its current tenant and cannot insert or update. `audit_events` — RLS, the app may only INSERT, with `tenant_id` equal to the current tenant or NULL (pre-authentication events); it has no SELECT privilege.

Column privileges add a second layer: the app cannot read `users.password_hash` and cannot UPDATE any `tenant_id` column.

**Transaction context.** `tenant_transaction(engine, tenant_id, user_id)` begins a transaction and runs `set_config('app.tenant_id', …, true)` and `set_config('app.user_id', …, true)`. `true` makes them transaction-local. `app_current_tenant_id()` returns `NULL` for a missing or empty setting, so a transaction without context sees and writes no tenant rows. The tenant id comes only from a verified token or the restricted login lookup (ADR 0101), never from request data.

**Pool cleanup.** The app engine uses `pool_reset_on_return="rollback"` and, on every checkin, rolls back and runs `DISCARD ALL` in autocommit, so even a buggy session-level `SET` cannot reach the next borrower. psycopg's automatic server-side prepared statements are disabled (`prepare_threshold=None`) because `DISCARD ALL` deallocates them.

**Cross-tenant references.** Every parent has `UNIQUE (tenant_id, id)`, and every child references it with a composite foreign key `(tenant_id, x_id)`. A row in tenant A cannot reference a user, group, role or resource of tenant B, whichever role writes it (verified for the owner role, which bypasses RLS). Nullable principal columns on `resource_grants` use `MATCH SIMPLE` composite FKs, with `CHECK (num_nonnulls(user_id, group_id) = 1)`.

**Restricted definer functions.** `auth_lookup_login` (ADR 0101), `purge_expired_sessions` and the `bump_policy_epoch` trigger run as the owner, with `search_path` pinned to `pg_catalog, public` and `EXECUTE` revoked from PUBLIC.

**Out of scope for M0.** Model-generated SQL is not introduced. M2 adds a separate read-only query role that is not the owner and lacks BYPASSRLS, uses security-invoker views, and blocks `set_config` and other session-changing functions; that role must not be able to set `app.tenant_id` itself.

## Consequences

Any role can call `set_config` on a custom setting. The guarantee is therefore that *application code* sets the context only from verified identity; the M2 SQL role must not be given a path to execute arbitrary statements without SQL validation.
