"""M6 review: receipt-history canaries for Mailpit reconciliation.

Reconciliation may treat "absent from Mailpit" as "never delivered" only while the
newest canary accepted before the send is still present in Mailpit. Otherwise Mailpit's
history does not cover the send (store reset, pruned or not persistent) and the send
stays uncertain. See docs/adr/0601-email-approval-and-outbox.md.
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0013_mail_receipt_canaries"
down_revision = "0012_wave3_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
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


def downgrade() -> None:
    op.execute("DROP TABLE mail_receipt_canaries")
