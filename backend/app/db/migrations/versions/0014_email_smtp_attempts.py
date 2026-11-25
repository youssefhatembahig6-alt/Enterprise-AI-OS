"""M6 review round 2: durable SMTP-start records replace receipt-history canaries.

A surviving canary does not prove that a missing email was never accepted (only that
email may have been deleted). Recovery now re-sends an uncertain email only when no
SMTP-start record exists for its current attempt, i.e. the attempt provably never
reached SMTP. See docs/adr/0601-email-approval-and-outbox.md.
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0014_email_smtp_attempts"
down_revision = "0013_mail_receipt_canaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP TABLE mail_receipt_canaries;
        -- Written and committed on its own connection immediately before an attempt
        -- opens SMTP. No foreign key to email_outbox: the handoff transaction holds the
        -- outbox row lock while this row is inserted.
        CREATE TABLE email_smtp_attempts (
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            execution_key uuid NOT NULL,
            attempt integer NOT NULL CHECK (attempt > 0),
            started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id, execution_key, attempt)
        );
        CREATE INDEX email_smtp_attempts_key ON email_smtp_attempts (execution_key, attempt);
        ALTER TABLE email_smtp_attempts ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON email_smtp_attempts
            USING (tenant_id = app_current_tenant_id())
            WITH CHECK (tenant_id = app_current_tenant_id());
        """
    )
    op.execute(f"GRANT SELECT, INSERT ON email_smtp_attempts TO {app_role_name()}")


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP TABLE email_smtp_attempts;
        CREATE TABLE mail_receipt_canaries (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id),
            message_id text NOT NULL UNIQUE,
            recipient text NOT NULL,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'missing')),
            accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            checked_at timestamptz,
            UNIQUE (tenant_id, id)
        );
        CREATE UNIQUE INDEX mail_receipt_canaries_one_active
            ON mail_receipt_canaries (tenant_id) WHERE status = 'active';
        ALTER TABLE mail_receipt_canaries ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON mail_receipt_canaries
            USING (tenant_id = app_current_tenant_id())
            WITH CHECK (tenant_id = app_current_tenant_id());
        """
    )
    app = app_role_name()
    op.execute(f"GRANT SELECT, INSERT ON mail_receipt_canaries TO {app}")
    op.execute(f"GRANT UPDATE (status, checked_at) ON mail_receipt_canaries TO {app}")
