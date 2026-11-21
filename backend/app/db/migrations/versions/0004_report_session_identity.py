"""M2 review fix: bind the report query context to a database session identity.

Revision 0003 keyed the context row on the backend PID alone. A crashed worker could
leave a row behind, and a later query-role connection that the operating system gave
the same PID could inherit it until it expired. This revision binds the context to:

* the bound session's identity ``(pg_backend_pid(), backend_start)``. The query
  connection reports its own ``backend_start`` (a session may read its own; other
  roles see NULL). A new backend that reuses the PID has a different start time.
* a per-binding secret. The application generates it, stores only its SHA-256 in the
  context row and sets it in the bound transaction with ``SET LOCAL
  eaios.report_ctx``. It is never present on any other session and vanishes at
  COMMIT, ROLLBACK or ``DISCARD ALL``.

The RLS policies evaluate ``reporting_ctx.report_context_tenant()`` once per query
(scalar subquery). That SECURITY INVOKER wrapper reads the caller's own session start
and secret and passes them to the SECURITY DEFINER lookup, which also requires
``pg_backend_pid()`` and ``session_user`` to match and the row to be unexpired.
Opening a context deletes expired rows and rows of backends that no longer exist.

Revision ID: 0004_report_session_identity
Revises: 0003_verified_reports
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name, report_role_name

revision = "0004_report_session_identity"
down_revision = "0003_verified_reports"
branch_labels = None
depends_on = None

SALES_TABLES = (
    "sales_customers",
    "sales_products",
    "sales_orders",
    "sales_order_lines",
    "sales_invoices",
)


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def _policies(report: str, expression: str) -> None:
    for table in SALES_TABLES:
        _run(f"DROP POLICY IF EXISTS report_context_read ON {table}")
        _run(f"DROP POLICY IF EXISTS report_context_restrict ON {table}")
        _run(
            f"CREATE POLICY report_context_read ON {table} FOR SELECT TO {report}"
            f" USING (tenant_id = {expression})"
        )
        _run(
            f"CREATE POLICY report_context_restrict ON {table} AS RESTRICTIVE FOR SELECT"
            f" TO {report} USING (tenant_id = {expression})"
        )


def upgrade() -> None:
    app, report = app_role_name(), report_role_name()
    _run(
        f"""
DELETE FROM report_query_contexts;
ALTER TABLE report_query_contexts
    ADD COLUMN backend_start timestamptz NOT NULL,
    ADD COLUMN secret_sha256 text NOT NULL CHECK (secret_sha256 ~ '^[0-9a-f]{{64}}$');

DROP FUNCTION report_context_open(integer, uuid, integer);
CREATE FUNCTION report_context_open(
        p_backend_pid integer, p_backend_start timestamptz, p_secret_sha256 text,
        p_run_id uuid, p_ttl_seconds integer)
    RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
DECLARE
    v_tenant uuid := public.app_current_tenant_id();
    v_user uuid := nullif(current_setting('app.user_id', true), '')::uuid;
BEGIN
    IF v_tenant IS NULL OR v_user IS NULL THEN
        RAISE EXCEPTION 'report context requires a trusted tenant and user';
    END IF;
    IF p_ttl_seconds < 1 OR p_ttl_seconds > 120 THEN
        RAISE EXCEPTION 'report context ttl out of range';
    END IF;
    IF p_backend_start IS NULL OR p_secret_sha256 !~ '^[0-9a-f]{{64}}$' THEN
        RAISE EXCEPTION 'report context needs a session start and a secret digest';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_stat_activity
         WHERE pid = p_backend_pid AND usename = '{report}' AND datname = current_database()
    ) THEN
        RAISE EXCEPTION 'backend % is not a report query connection', p_backend_pid;
    END IF;
    -- Crash cleanup: expired rows and rows of backends that no longer exist.
    DELETE FROM public.report_query_contexts c
     WHERE c.expires_at <= clock_timestamp()
        OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_stat_activity a WHERE a.pid = c.backend_pid);
    INSERT INTO public.report_query_contexts
        (backend_pid, backend_start, secret_sha256, tenant_id, user_id, run_id, expires_at)
    VALUES (p_backend_pid, p_backend_start, p_secret_sha256, v_tenant, v_user, p_run_id,
            clock_timestamp() + make_interval(secs => p_ttl_seconds))
    ON CONFLICT (backend_pid) DO UPDATE
       SET backend_start = EXCLUDED.backend_start, secret_sha256 = EXCLUDED.secret_sha256,
           tenant_id = EXCLUDED.tenant_id, user_id = EXCLUDED.user_id,
           run_id = EXCLUDED.run_id, expires_at = EXCLUDED.expires_at;
END
$$;
REVOKE ALL ON FUNCTION report_context_open(integer, timestamptz, text, uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION report_context_open(integer, timestamptz, text, uuid, integer) TO {app};

-- Definer lookup. Without the matching secret it returns NULL, whatever start time a
-- caller claims; the PID and login role are taken from the session itself.
CREATE FUNCTION reporting_ctx.report_context_lookup(p_backend_start timestamptz, p_secret text)
    RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT c.tenant_id FROM public.report_query_contexts c
     WHERE c.backend_pid = pg_backend_pid()
       AND c.backend_start = p_backend_start
       AND p_secret IS NOT NULL AND length(p_secret) >= 32
       AND c.secret_sha256 = encode(sha256(convert_to(p_secret, 'UTF8')), 'hex')
       AND session_user = '{report}'
       AND c.expires_at > clock_timestamp()
$$;

-- Invoker wrapper used by the RLS policies: the caller's own session start (visible
-- to the session itself) and the transaction-local secret.
DROP FUNCTION IF EXISTS reporting_ctx.report_context_tenant() CASCADE;
CREATE FUNCTION reporting_ctx.report_context_tenant() RETURNS uuid
    LANGUAGE sql STABLE SECURITY INVOKER
    SET search_path = pg_catalog
    AS $$
    SELECT reporting_ctx.report_context_lookup(
        (SELECT a.backend_start FROM pg_catalog.pg_stat_get_activity(pg_catalog.pg_backend_pid()) a),
        nullif(pg_catalog.current_setting('eaios.report_ctx', true), ''))
$$;

REVOKE ALL ON FUNCTION reporting_ctx.report_context_lookup(timestamptz, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION reporting_ctx.report_context_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reporting_ctx.report_context_lookup(timestamptz, text) TO {report};
GRANT EXECUTE ON FUNCTION reporting_ctx.report_context_tenant() TO {report};
"""
    )
    # Scalar subquery: evaluated once per query as an InitPlan, not per row.
    _policies(report, "(SELECT reporting_ctx.report_context_tenant())")


def downgrade() -> None:
    app, report = app_role_name(), report_role_name()
    _run(
        f"""
DELETE FROM report_query_contexts;
DROP FUNCTION IF EXISTS reporting_ctx.report_context_tenant() CASCADE;
DROP FUNCTION IF EXISTS reporting_ctx.report_context_lookup(timestamptz, text);
DROP FUNCTION IF EXISTS report_context_open(integer, timestamptz, text, uuid, integer);
ALTER TABLE report_query_contexts DROP COLUMN backend_start, DROP COLUMN secret_sha256;
CREATE FUNCTION report_context_open(p_backend_pid integer, p_run_id uuid, p_ttl_seconds integer)
    RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
DECLARE
    v_tenant uuid := public.app_current_tenant_id();
    v_user uuid := nullif(current_setting('app.user_id', true), '')::uuid;
BEGIN
    IF v_tenant IS NULL OR v_user IS NULL THEN
        RAISE EXCEPTION 'report context requires a trusted tenant and user';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_stat_activity
         WHERE pid = p_backend_pid AND usename = '{report}' AND datname = current_database()
    ) THEN
        RAISE EXCEPTION 'backend % is not a report query connection', p_backend_pid;
    END IF;
    INSERT INTO public.report_query_contexts (backend_pid, tenant_id, user_id, run_id, expires_at)
    VALUES (p_backend_pid, v_tenant, v_user, p_run_id,
            clock_timestamp() + make_interval(secs => p_ttl_seconds))
    ON CONFLICT (backend_pid) DO UPDATE
       SET tenant_id = EXCLUDED.tenant_id, user_id = EXCLUDED.user_id,
           run_id = EXCLUDED.run_id, expires_at = EXCLUDED.expires_at;
END
$$;
REVOKE ALL ON FUNCTION report_context_open(integer, uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION report_context_open(integer, uuid, integer) TO {app};
CREATE FUNCTION reporting_ctx.report_context_tenant() RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT c.tenant_id FROM public.report_query_contexts c
     WHERE c.backend_pid = pg_backend_pid()
       AND session_user = '{report}'
       AND c.expires_at > clock_timestamp()
$$;
REVOKE ALL ON FUNCTION reporting_ctx.report_context_tenant() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reporting_ctx.report_context_tenant() TO {report};
"""
    )
    _policies(report, "reporting_ctx.report_context_tenant()")
