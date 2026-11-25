"""M6 wave 3 actions: versioned email drafts, actor-bound approvals, a durable email
outbox with an explicit uncertain state, and review-only scheduled briefings.

Design: docs/adr/0601-email-approval-and-outbox.md, docs/adr/0602-scheduled-briefings.md.
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0012_wave3_actions"
down_revision = "0011_m5_wave2"
branch_labels = None
depends_on = None

TABLES = (
    "email_actions",
    "email_draft_versions",
    "email_approvals",
    "email_outbox",
    "briefing_schedules",
    "briefing_occurrences",
)

SCHEMA = r"""
ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
    CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports', 'legal', 'code',
                                  'actions']::text[]);
ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_connectors_check;
ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_connectors_check
    CHECK (enabled_connectors <@ ARRAY['upload', 'google_drive', 'github', 'local_folder',
                                       'mailpit']::text[]);

-- One email action per draft. ``version`` is the current draft version; every edit
-- inserts an immutable email_draft_versions row and bumps it.
CREATE TABLE email_actions (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    owner_user_id uuid NOT NULL,
    source_run_id uuid NOT NULL,
    origin text NOT NULL DEFAULT 'user' CHECK (origin IN ('user', 'briefing')),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN
        ('draft', 'approved', 'queued', 'sending', 'uncertain', 'delivered', 'blocked',
         'failed', 'cancelled')),
    status_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users(tenant_id, id),
    FOREIGN KEY (tenant_id, source_run_id) REFERENCES report_runs(tenant_id, id)
);
CREATE INDEX email_actions_owner ON email_actions (tenant_id, owner_user_id, created_at DESC);

CREATE TABLE email_draft_versions (
    tenant_id uuid NOT NULL,
    action_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    recipient_user_id uuid NOT NULL,
    recipient_email text NOT NULL CHECK (recipient_email !~ '[\r\n,;<>]'),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 200
                                 AND subject !~ '[\r\n]'),
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 50000),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    draft_sha256 text NOT NULL CHECK (draft_sha256 ~ '^[0-9a-f]{64}$'),
    edited_by uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, action_id, version),
    FOREIGN KEY (tenant_id, action_id) REFERENCES email_actions(tenant_id, id),
    FOREIGN KEY (tenant_id, recipient_user_id) REFERENCES users(tenant_id, id),
    FOREIGN KEY (tenant_id, edited_by) REFERENCES users(tenant_id, id)
);
CREATE TRIGGER email_draft_versions_immutable BEFORE UPDATE OR DELETE ON email_draft_versions
    FOR EACH ROW EXECUTE FUNCTION report_reject_mutation();

-- An approval binds actor, session, tenant and the exact draft version and hash. Only
-- the SHA-256 of the one-time approval token is stored.
CREATE TABLE email_approvals (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    action_id uuid NOT NULL,
    version integer NOT NULL,
    draft_sha256 text NOT NULL,
    actor_user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'consumed', 'invalidated')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    consumed_at timestamptz,
    UNIQUE (tenant_id, id),
    CHECK (expires_at > created_at),
    FOREIGN KEY (tenant_id, action_id, version)
        REFERENCES email_draft_versions(tenant_id, action_id, version),
    FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, id)
);
-- At most one live approval per action.
CREATE UNIQUE INDEX email_approvals_one_active ON email_approvals (tenant_id, action_id)
    WHERE status = 'active';

-- Durable outbox. One row per action, ever: an action is sent at most once by this
-- application. ``execution_key`` is the unique execution identity and also forms the
-- RFC 5322 Message-ID used for reconciliation against Mailpit.
CREATE TABLE email_outbox (
    execution_key uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    action_id uuid NOT NULL,
    version integer NOT NULL,
    approval_id uuid NOT NULL,
    message_id text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'dispatching', 'uncertain', 'delivered', 'blocked', 'failed',
         'cancelled')),
    status_reason text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    reconcile_checks integer NOT NULL DEFAULT 0 CHECK (reconcile_checks >= 0),
    dispatched_at timestamptz,
    claimed_at timestamptz,
    smtp_accepted_at timestamptz,
    uncertain_since timestamptz,
    confirmed_at timestamptz,
    mailpit_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, action_id),
    UNIQUE (tenant_id, approval_id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES email_approvals(tenant_id, id),
    FOREIGN KEY (tenant_id, action_id, version)
        REFERENCES email_draft_versions(tenant_id, action_id, version)
);

-- Scheduled briefings: the owner's question re-run with the owner's *current* identity
-- and permissions at every occurrence. Output becomes a draft for review; nothing here
-- can approve or send.
CREATE TABLE briefing_schedules (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    owner_user_id uuid NOT NULL,
    recipient_user_id uuid NOT NULL,
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    interval_seconds integer NOT NULL CHECK (interval_seconds BETWEEN 3600 AND 2678400),
    next_run_at timestamptz NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users(tenant_id, id),
    FOREIGN KEY (tenant_id, recipient_user_id) REFERENCES users(tenant_id, id)
);
CREATE TABLE briefing_occurrences (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    schedule_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    policy_epoch integer,
    run_id uuid,
    action_id uuid,
    status text NOT NULL CHECK (status IN ('running', 'review', 'blocked', 'failed')),
    status_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, schedule_id, due_at),
    FOREIGN KEY (tenant_id, schedule_id) REFERENCES briefing_schedules(tenant_id, id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs(tenant_id, id),
    FOREIGN KEY (tenant_id, action_id) REFERENCES email_actions(tenant_id, id)
);

-- Workers discover identifiers only (across tenants), then enter tenant RLS for every
-- read and write of content. Returns nothing but ids and the kind of work.
CREATE FUNCTION action_work_due(lease_seconds integer, redispatch_seconds integer)
    RETURNS TABLE(tenant_id uuid, id uuid, kind text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    (SELECT o.tenant_id, o.execution_key, 'deliver'::text FROM public.email_outbox o
      WHERE o.status = 'pending'
        AND (o.dispatched_at IS NULL
             OR o.dispatched_at < clock_timestamp() - make_interval(secs => redispatch_seconds))
      ORDER BY o.created_at LIMIT 50)
    UNION ALL
    (SELECT o.tenant_id, o.execution_key, 'stale'::text FROM public.email_outbox o
      WHERE o.status = 'dispatching'
        AND o.claimed_at < clock_timestamp() - make_interval(secs => lease_seconds)
      LIMIT 50)
    UNION ALL
    (SELECT o.tenant_id, o.execution_key, 'reconcile'::text FROM public.email_outbox o
      WHERE o.status = 'uncertain' LIMIT 50)
    UNION ALL
    (SELECT s.tenant_id, s.id, 'schedule'::text FROM public.briefing_schedules s
      WHERE s.active AND s.next_run_at <= clock_timestamp() LIMIT 50)
    UNION ALL
    (SELECT b.tenant_id, b.id, 'briefing'::text FROM public.briefing_occurrences b
      WHERE b.status = 'running' LIMIT 50)
$$;
REVOKE ALL ON FUNCTION action_work_due(integer, integer) FROM PUBLIC;
"""

UPDATABLE = (
    ("email_actions", "version, status, status_reason, updated_at"),
    ("email_approvals", "status, consumed_at"),
    (
        "email_outbox",
        "status, status_reason, attempts, reconcile_checks, dispatched_at, claimed_at,"
        " smtp_accepted_at, uncertain_since, confirmed_at, mailpit_id, updated_at",
    ),
    ("briefing_schedules", "active, next_run_at, updated_at"),
    ("briefing_occurrences", "status, status_reason, run_id, action_id, updated_at"),
)


def upgrade() -> None:
    op.get_bind().exec_driver_sql(SCHEMA)
    app = app_role_name()
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {app}")
    for table, columns in UPDATABLE:
        op.execute(f"GRANT UPDATE ({columns}) ON {table} TO {app}")
    op.execute(f"GRANT EXECUTE ON FUNCTION action_work_due(integer, integer) TO {app}")


def downgrade() -> None:
    op.execute("DROP FUNCTION action_work_due(integer, integer)")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
    op.get_bind().exec_driver_sql(
        """
        UPDATE tenant_settings SET
            enabled_packs = array_remove(enabled_packs, 'actions'),
            enabled_connectors = array_remove(enabled_connectors, 'mailpit');
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
            CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports', 'legal', 'code']::text[]);
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_connectors_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_connectors_check
            CHECK (enabled_connectors <@ ARRAY['upload', 'google_drive', 'github',
                                               'local_folder']::text[]);
        """
    )
