"""M3: store parsed canonical elements with each document version.

A version's canonical text was already immutable; its element spans (type, code-point
start/end, page, heading path, table header end) are now stored too. Reindexing and
retries rebuild chunks from the stored elements instead of re-parsing, so citation
offsets never move and a version parsed by Docling stays Docling-parsed even if the
parser is unavailable later.

Revision ID: 0006_document_elements
Revises: 0005_documents
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0006_document_elements"
down_revision = "0005_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    app = app_role_name()
    bind = op.get_bind().execution_options(no_parameters=True)
    bind.exec_driver_sql("ALTER TABLE document_versions ADD COLUMN elements jsonb")
    bind.exec_driver_sql(
        """
        CREATE OR REPLACE FUNCTION document_version_guard() RETURNS trigger
            LANGUAGE plpgsql
            AS $$
        BEGIN
            IF OLD.canonical_text IS NOT NULL AND (
                NEW.canonical_text IS DISTINCT FROM OLD.canonical_text
                OR NEW.text_sha256 IS DISTINCT FROM OLD.text_sha256
                OR NEW.byte_sha256 IS DISTINCT FROM OLD.byte_sha256
                OR (OLD.elements IS NOT NULL AND NEW.elements IS DISTINCT FROM OLD.elements)) THEN
                RAISE EXCEPTION 'document version text is immutable';
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    bind.exec_driver_sql(f"GRANT UPDATE (elements) ON document_versions TO {app}")


def downgrade() -> None:
    op.get_bind().exec_driver_sql("ALTER TABLE document_versions DROP COLUMN elements")
