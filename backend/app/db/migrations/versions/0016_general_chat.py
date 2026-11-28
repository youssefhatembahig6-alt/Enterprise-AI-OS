"""M8: General chat mode.

Adds the ``general`` pack to the tenant settings check and ``general`` to the report run
kinds. A general run is answered by the chat model alone (no documents, data or actions),
so it needs no new table: it is a normal run with owner-only replay.

Downgrade removes general runs (their events, jobs and answer rows cascade; general runs
are never email sources) and the pack from every tenant, then restores both checks.
"""

from alembic import op

revision = "0016_general_chat"
down_revision = "0015_backfill_smtp_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
            CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports', 'legal', 'code',
                                          'actions', 'general']::text[]);
        ALTER TABLE report_runs DROP CONSTRAINT report_runs_kind_check;
        ALTER TABLE report_runs ADD CONSTRAINT report_runs_kind_check
            CHECK (kind IN ('report', 'documents', 'legal', 'code', 'general'));
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DELETE FROM report_runs WHERE kind = 'general';
        UPDATE tenant_settings SET enabled_packs = array_remove(enabled_packs, 'general');
        ALTER TABLE report_runs DROP CONSTRAINT report_runs_kind_check;
        ALTER TABLE report_runs ADD CONSTRAINT report_runs_kind_check
            CHECK (kind IN ('report', 'documents', 'legal', 'code'));
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
            CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports', 'legal', 'code',
                                          'actions']::text[]);
        """
    )
