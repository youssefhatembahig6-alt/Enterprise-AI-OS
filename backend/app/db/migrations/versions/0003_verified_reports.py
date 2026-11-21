"""M2 verified SQL reports: dataset resources and capability, tenant report profiles,
runs/events/evidence/jobs, the restricted ``reporting`` view surface and the
separate read-only query role with an unforgeable per-backend query context.

Security model (see docs/adr/0201-restricted-report-sql.md):

* The query role (``EAIOS_REPORT_DB_ROLE``) is created by a superuser outside
  migrations. This migration refuses to run if it is missing, is a superuser, has
  BYPASSRLS/CREATEROLE/CREATEDB, or owns anything.
* It may only read the ``reporting`` schema's security-invoker views and the
  matching base-table columns. It has no USAGE on ``public`` and no other grants.
* Row-Level Security on the sales tables gives the query role a permissive and a
  *restrictive* policy, both requiring ``report_context_tenant()``. That function
  reads a context row keyed by the query connection's backend PID, which only the
  application role can write (``report_context_open``/``close``). Changing a session
  variable such as ``app.tenant_id`` therefore cannot widen what the query role sees.

Revision ID: 0003_verified_reports
Revises: 0002_business_sales
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name, report_role_name

revision = "0003_verified_reports"
down_revision = "0002_business_sales"
branch_labels = None
depends_on = None

SALES_TABLES = (
    "sales_customers",
    "sales_products",
    "sales_orders",
    "sales_order_lines",
    "sales_invoices",
)

# view name -> (base table, exposed columns). tenant_id is intentionally not exposed.
REPORTING_VIEWS = {
    "sales_orders": (
        "sales_orders",
        "id, order_number, customer_id, order_date, region, status, subtotal, tax, total, currency",
    ),
    "sales_order_lines": (
        "sales_order_lines",
        "id, order_id, product_id, quantity, unit_price, line_total",
    ),
    "sales_invoices": (
        "sales_invoices",
        "id, order_id, invoice_number, issue_date, due_date, amount, currency, status",
    ),
    "sales_products": ("sales_products", "id, sku, name, tier, unit_price, currency, is_active"),
    "sales_customers": ("sales_customers", "id, name, region, country, since_date"),
}

NEW_TENANT_TABLES = (
    "tenant_report_profiles",
    "report_runs",
    "report_run_events",
    "report_evidence",
    "report_jobs",
)


def _preflight(report: str) -> str:
    return f"""
DO $$
DECLARE
    r record;
    owned integer;
BEGIN
    SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolcanlogin, oid
      INTO r FROM pg_roles WHERE rolname = '{report}';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'report query role "{report}" does not exist; create it first '
            '(see docs/runbook.md: scripts/dev.py add-report-role)';
    END IF;
    IF r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb THEN
        RAISE EXCEPTION 'report query role "{report}" must be NOSUPERUSER NOBYPASSRLS '
            'NOCREATEROLE NOCREATEDB';
    END IF;
    SELECT count(*) INTO owned FROM pg_class WHERE relowner = r.oid;
    IF owned > 0 THEN
        RAISE EXCEPTION 'report query role "{report}" must not own relations';
    END IF;
END
$$;
"""


def _schema(report: str) -> str:
    return rf"""
-- Dataset resources and the dataset.query capability/grant action.
ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
ALTER TABLE resources ADD CONSTRAINT resources_kind_check CHECK (kind IN ('document', 'dataset'));
ALTER TABLE role_capabilities DROP CONSTRAINT role_capabilities_capability_check;
ALTER TABLE role_capabilities ADD CONSTRAINT role_capabilities_capability_check
    CHECK (capability IN ('resource.read', 'resource.admin', 'dataset.query'));
ALTER TABLE resource_grants DROP CONSTRAINT resource_grants_action_check;
ALTER TABLE resource_grants ADD CONSTRAINT resource_grants_action_check
    CHECK (action IN ('resource.read', 'dataset.query'));

-- Tenant configuration for a supported report. Behaviour depends on this row,
-- never on the tenant's name.
CREATE TABLE tenant_report_profiles (
    tenant_id    uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    report_key   text NOT NULL CHECK (report_key ~ '^[a-z][a-z0-9_.]{{0,62}}$'),
    resource_id  uuid NOT NULL,
    version      integer NOT NULL CHECK (version >= 1),
    config       jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, report_key),
    FOREIGN KEY (tenant_id, resource_id) REFERENCES resources (tenant_id, id)
);

CREATE TABLE report_runs (
    id                          uuid PRIMARY KEY,
    tenant_id                   uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    owner_user_id               uuid NOT NULL,
    resource_id                 uuid,
    report_key                  text,
    question                    text NOT NULL CHECK (char_length(question) BETWEEN 1 AND 1000),
    client_request_id           text NOT NULL CHECK (client_request_id ~ '^[A-Za-z0-9_-]{{8,64}}$'),
    status                      text NOT NULL CHECK (status IN
                                ('queued', 'running', 'published', 'abstained', 'failed', 'cancelled')),
    cancel_requested            boolean NOT NULL DEFAULT false,
    inference_mode              text NOT NULL CHECK (inference_mode IN ('gateway', 'fake')),
    policy_epoch_at_start       bigint NOT NULL,
    policy_epoch_at_publication bigint,
    outcome_code                text,
    result                      jsonb,
    stats                       jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    last_seq                    bigint NOT NULL DEFAULT 0,
    created_at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at                  timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at                 timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, owner_user_id, client_request_id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, resource_id) REFERENCES resources (tenant_id, id),
    CHECK ((status = 'published') = (result IS NOT NULL))
);
CREATE INDEX report_runs_owner_idx ON report_runs (tenant_id, owner_user_id, created_at);

CREATE TABLE report_run_events (
    tenant_id   uuid NOT NULL,
    run_id      uuid NOT NULL,
    seq         bigint NOT NULL CHECK (seq >= 1),
    event_type  text NOT NULL CHECK (event_type IN
                ('status', 'token', 'citation', 'approval_required', 'error', 'done')),
    version     smallint NOT NULL DEFAULT 1,
    payload     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs (tenant_id, id) ON DELETE CASCADE
);
-- Exactly one terminal event per run, even if a job is retried.
CREATE UNIQUE INDEX report_run_events_single_done ON report_run_events (run_id)
    WHERE event_type = 'done';

-- Immutable, exact query results that report cells and citations refer to.
CREATE TABLE report_evidence (
    id                   uuid PRIMARY KEY,
    tenant_id            uuid NOT NULL,
    run_id               uuid NOT NULL,
    owner_user_id        uuid NOT NULL,
    resource_id          uuid NOT NULL,
    report_key           text NOT NULL,
    model_sql            text NOT NULL,
    executed_sql         text NOT NULL,
    parameters           jsonb NOT NULL,
    columns              jsonb NOT NULL,
    rows                 jsonb NOT NULL,
    row_count            integer NOT NULL CHECK (row_count >= 0),
    semantics            jsonb NOT NULL,
    dataset_fingerprint  text NOT NULL,
    policy_epoch         bigint NOT NULL,
    executed_at          timestamptz NOT NULL,
    result_sha256        text NOT NULL CHECK (result_sha256 ~ '^[0-9a-f]{{64}}$'),
    created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, resource_id) REFERENCES resources (tenant_id, id)
);
CREATE INDEX report_evidence_run_idx ON report_evidence (tenant_id, run_id);

CREATE FUNCTION report_reject_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END
$$;
CREATE TRIGGER report_evidence_immutable BEFORE UPDATE ON report_evidence
    FOR EACH ROW EXECUTE FUNCTION report_reject_mutation();
CREATE TRIGGER report_run_events_immutable BEFORE UPDATE ON report_run_events
    FOR EACH ROW EXECUTE FUNCTION report_reject_mutation();

-- Durable job/outbox row, written in the same transaction as the run.
CREATE TABLE report_jobs (
    run_id            uuid PRIMARY KEY,
    tenant_id         uuid NOT NULL,
    status            text NOT NULL CHECK (status IN ('pending', 'claimed', 'done')),
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_until     timestamptz,
    last_dispatched_at timestamptz,
    created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs (tenant_id, id) ON DELETE CASCADE
);

-- Dispatcher view across tenants: returns identifiers only, never content.
CREATE FUNCTION report_jobs_due(p_limit integer, p_redispatch_after interval)
    RETURNS TABLE (run_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT j.run_id, j.tenant_id FROM public.report_jobs j
     WHERE (j.status = 'pending'
            AND (j.last_dispatched_at IS NULL
                 OR j.last_dispatched_at < clock_timestamp() - p_redispatch_after))
        OR (j.status = 'claimed' AND j.claimed_until < clock_timestamp())
     ORDER BY j.created_at
     LIMIT least(greatest(p_limit, 0), 100)
$$;

-- Publication boundary: a share lock on the caller's tenant row conflicts with the
-- policy-epoch trigger, so a concurrent authorization change either commits before
-- this (and is seen by the re-check) or waits until publication commits.
CREATE FUNCTION lock_tenant_policy_epoch() RETURNS bigint
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT policy_epoch FROM public.tenants
     WHERE id = public.app_current_tenant_id()
       FOR SHARE
$$;

-- Query-role context: one row per query-role backend, written only by the app role.
CREATE TABLE report_query_contexts (
    backend_pid  integer PRIMARY KEY,
    tenant_id    uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id      uuid NOT NULL,
    run_id       uuid NOT NULL,
    expires_at   timestamptz NOT NULL
);

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
    IF p_ttl_seconds < 1 OR p_ttl_seconds > 120 THEN
        RAISE EXCEPTION 'report context ttl out of range';
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

CREATE FUNCTION report_context_close(p_backend_pid integer) RETURNS void
    LANGUAGE sql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    DELETE FROM public.report_query_contexts WHERE backend_pid = p_backend_pid
$$;

-- Evaluated by the query role's RLS policies. session_user is the login role, so a
-- different role cannot borrow another backend's context. It lives in its own schema
-- because the query role has no USAGE on public; the executor calls it to confirm
-- the bound context, and the SQL allowlist never permits schema-qualified functions.
CREATE SCHEMA reporting_ctx;
REVOKE ALL ON SCHEMA reporting_ctx FROM PUBLIC;
CREATE FUNCTION reporting_ctx.report_context_tenant() RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT c.tenant_id FROM public.report_query_contexts c
     WHERE c.backend_pid = pg_backend_pid()
       AND session_user = '{report}'
       AND c.expires_at > clock_timestamp()
$$;

REVOKE ALL ON FUNCTION report_jobs_due(integer, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION lock_tenant_policy_epoch() FROM PUBLIC;
REVOKE ALL ON FUNCTION report_context_open(integer, uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION report_context_close(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION reporting_ctx.report_context_tenant() FROM PUBLIC;
REVOKE ALL ON FUNCTION report_reject_mutation() FROM PUBLIC;

CREATE SCHEMA reporting;
REVOKE ALL ON SCHEMA reporting FROM PUBLIC;
"""


def _grants(app: str, report: str) -> str:
    return f"""
-- Only named roles may resolve objects in public. The query role gets no USAGE, so it
-- cannot even name application tables or functions (its RLS functions are bound by OID).
REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {app};

GRANT SELECT ON tenant_report_profiles TO {app};
GRANT INSERT, SELECT ON report_runs TO {app};
GRANT UPDATE (status, cancel_requested, resource_id, report_key, policy_epoch_at_publication,
              outcome_code, result, stats, last_seq, updated_at, finished_at) ON report_runs TO {app};
GRANT INSERT, SELECT ON report_run_events TO {app};
GRANT INSERT, SELECT ON report_evidence TO {app};
GRANT INSERT, SELECT ON report_jobs TO {app};
GRANT UPDATE (status, attempts, claimed_until, last_dispatched_at, updated_at) ON report_jobs TO {app};
GRANT EXECUTE ON FUNCTION report_jobs_due(integer, interval) TO {app};
GRANT EXECUTE ON FUNCTION lock_tenant_policy_epoch() TO {app};
GRANT EXECUTE ON FUNCTION report_context_open(integer, uuid, integer) TO {app};
GRANT EXECUTE ON FUNCTION report_context_close(integer) TO {app};

-- The query role: reporting views only, plus the base columns those views expose.
GRANT USAGE ON SCHEMA reporting TO {report};
GRANT USAGE ON SCHEMA reporting_ctx TO {report};
GRANT EXECUTE ON FUNCTION reporting_ctx.report_context_tenant() TO {report};
"""


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app, report = app_role_name(), report_role_name()
    _run(_preflight(report))
    _run(_schema(report))
    for table in NEW_TENANT_TABLES:
        _run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _run(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
    # Context rows are reachable only through the SECURITY DEFINER functions.
    _run("ALTER TABLE report_query_contexts ENABLE ROW LEVEL SECURITY")
    for view, (table, columns) in REPORTING_VIEWS.items():
        _run(
            f"CREATE VIEW reporting.{view} WITH (security_invoker = true, security_barrier = true)"
            f" AS SELECT {columns} FROM public.{table}"
        )
        _run(f"GRANT SELECT ON reporting.{view} TO {report}")
        _run(f"GRANT SELECT ({columns}) ON public.{table} TO {report}")
    for table in SALES_TABLES:
        _run(
            f"CREATE POLICY report_context_read ON {table} FOR SELECT TO {report}"
            " USING (tenant_id = reporting_ctx.report_context_tenant())"
        )
        _run(
            f"CREATE POLICY report_context_restrict ON {table} AS RESTRICTIVE FOR SELECT"
            f" TO {report} USING (tenant_id = reporting_ctx.report_context_tenant())"
        )
    _run(_grants(app, report))


def downgrade() -> None:
    report = report_role_name()
    _run("GRANT USAGE ON SCHEMA public TO PUBLIC")
    for table in SALES_TABLES:
        _run(f"DROP POLICY IF EXISTS report_context_read ON {table}")
        _run(f"DROP POLICY IF EXISTS report_context_restrict ON {table}")
        _run(f"REVOKE ALL ON {table} FROM {report}")
    _run(
        f"""
        DROP SCHEMA IF EXISTS reporting CASCADE;
        DROP SCHEMA IF EXISTS reporting_ctx CASCADE;
        DROP TABLE IF EXISTS report_query_contexts, report_jobs, report_evidence,
            report_run_events, report_runs, tenant_report_profiles CASCADE;
        DROP FUNCTION IF EXISTS report_context_close(integer);
        DROP FUNCTION IF EXISTS report_context_open(integer, uuid, integer);
        DROP FUNCTION IF EXISTS lock_tenant_policy_epoch();
        DROP FUNCTION IF EXISTS report_jobs_due(integer, interval);
        DROP FUNCTION IF EXISTS report_reject_mutation();
        DELETE FROM resource_grants WHERE action = 'dataset.query';
        DELETE FROM role_capabilities WHERE capability = 'dataset.query';
        DELETE FROM resources WHERE kind = 'dataset';
        ALTER TABLE resource_grants DROP CONSTRAINT resource_grants_action_check;
        ALTER TABLE resource_grants ADD CONSTRAINT resource_grants_action_check
            CHECK (action IN ('resource.read'));
        ALTER TABLE role_capabilities DROP CONSTRAINT role_capabilities_capability_check;
        ALTER TABLE role_capabilities ADD CONSTRAINT role_capabilities_capability_check
            CHECK (capability IN ('resource.read', 'resource.admin'));
        ALTER TABLE resources DROP CONSTRAINT resources_kind_check;
        ALTER TABLE resources ADD CONSTRAINT resources_kind_check CHECK (kind IN ('document'));
        """
    )
