"""M8: private, persistent chat history.

``chat_conversations`` holds one row per conversation and ``chat_turns`` one row per
exchange: the employee's question, the handler the orchestrator chose, and either the run
that answered it (``run_id``) or, for a clarifying question, the reply text. Answers are
not copied: reopening a conversation replays each run through the run-events endpoint,
which re-checks the employee's current access, and citations are re-checked when opened.

Both tables carry ``owner_user_id`` and have two Row-Level Security policies: the usual
tenant isolation, and a RESTRICTIVE owner policy (``owner_user_id`` must equal the
session's ``app.user_id``), so even a query that forgot its owner filter cannot read,
change or delete another employee's history. Deleting a conversation cascades to its
turns; the runs themselves stay (approvals and the audit log refer to them).
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0017_chat_history"
down_revision = "0016_general_chat"
branch_labels = None
depends_on = None

TABLES = ("chat_conversations", "chat_turns")

SCHEMA = r"""
CREATE TABLE chat_conversations (
    id            uuid PRIMARY KEY,
    tenant_id     uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    owner_user_id uuid NOT NULL,
    title         text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX chat_conversations_owner_idx
    ON chat_conversations (tenant_id, owner_user_id, updated_at DESC);

CREATE TABLE chat_turns (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL,
    owner_user_id   uuid NOT NULL,
    conversation_id uuid NOT NULL,
    question        text NOT NULL CHECK (char_length(question) BETWEEN 1 AND 1000),
    handler         text NOT NULL CHECK (handler IN
                    ('general', 'documents', 'report', 'legal', 'code', 'clarify')),
    run_id          uuid,
    reply           text CHECK (reply IS NULL OR char_length(reply) <= 1000),
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, conversation_id)
        REFERENCES chat_conversations (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (tenant_id, run_id),
    -- A run-backed answer has no stored reply; a clarifying question has no run.
    CHECK ((handler = 'clarify') = (run_id IS NULL)),
    CHECK ((handler = 'clarify') = (reply IS NOT NULL))
);
CREATE INDEX chat_turns_conversation_idx ON chat_turns (tenant_id, conversation_id, created_at);
"""


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
        op.execute(
            f"CREATE POLICY owner_only ON {table} AS RESTRICTIVE"
            " USING (owner_user_id = nullif(current_setting('app.user_id', true), '')::uuid)"
            " WITH CHECK"
            " (owner_user_id = nullif(current_setting('app.user_id', true), '')::uuid)"
        )
        op.execute(f"GRANT SELECT, INSERT, DELETE ON {table} TO {app}")
    op.execute(f"GRANT UPDATE (title, updated_at) ON chat_conversations TO {app}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE {table}")
