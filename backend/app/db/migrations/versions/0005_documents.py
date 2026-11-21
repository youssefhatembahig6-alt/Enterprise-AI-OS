"""M3 authorized document answers: documents, immutable versions with canonical text,
index generations, chunks, durable ingestion jobs, answer sources, and an explicit
permission state on resources.

Invariants (docs/adr/0301-document-ingestion-and-generations.md):

* Permissions are saved with the resource *before* an ingestion job exists (same
  transaction). ``permission_state = 'unknown'`` keeps a resource ineligible.
* A version's bytes, canonical text and its SHA-256 never change after parsing.
* Chunks belong to one generation. At most one generation per document is active
  (partial unique index); activation is one transaction, so a crash leaves the
  previous generation searchable and never a mix.
* A deleted document keeps a tombstone (``documents.deleted_at`` and
  ``resources.status = 'deleted'``) so a late retry cannot resurrect it.

Revision ID: 0005_documents
Revises: 0004_report_session_identity
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0005_documents"
down_revision = "0004_report_session_identity"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = (
    "documents",
    "document_versions",
    "document_generations",
    "document_chunks",
    "ingestion_jobs",
    "answer_sources",
)

SCHEMA = r"""
ALTER TABLE resources ADD COLUMN permission_state text NOT NULL DEFAULT 'verified'
    CHECK (permission_state IN ('verified', 'unknown'));
DROP TRIGGER resources_policy_epoch ON resources;
CREATE TRIGGER resources_policy_epoch
    AFTER INSERT OR UPDATE OF classification, department, department_restricted,
        allowed_countries, status, permission_state OR DELETE ON resources
    FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();

ALTER TABLE report_runs ADD COLUMN kind text NOT NULL DEFAULT 'report'
    CHECK (kind IN ('report', 'documents'));

CREATE TABLE documents (
    tenant_id          uuid NOT NULL,
    id                 uuid NOT NULL,
    source             text NOT NULL DEFAULT 'upload' CHECK (source IN ('upload', 'fixture')),
    title              text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 300),
    current_version    integer NOT NULL CHECK (current_version >= 1),
    active_generation  integer,
    created_by         uuid,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    deleted_at         timestamptz,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, id) REFERENCES resources (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE document_versions (
    tenant_id       uuid NOT NULL,
    document_id     uuid NOT NULL,
    version         integer NOT NULL CHECK (version >= 1),
    filename        text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
    media_type      text NOT NULL,
    byte_size       integer NOT NULL CHECK (byte_size >= 0),
    byte_sha256     text NOT NULL CHECK (byte_sha256 ~ '^[0-9a-f]{64}$'),
    storage_key     text NOT NULL,
    status          text NOT NULL CHECK (status IN
                    ('queued', 'parsing', 'indexing', 'ready', 'failed', 'needs_ocr')),
    error_code      text,
    error_message   text,
    canonical_text  text,
    text_sha256     text CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    parser          text,
    parse_notes     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by      uuid,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, document_id, version),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE,
    CHECK ((canonical_text IS NULL) = (text_sha256 IS NULL))
);

-- Canonical text is immutable once set.
CREATE FUNCTION document_version_guard() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.canonical_text IS NOT NULL AND (
        NEW.canonical_text IS DISTINCT FROM OLD.canonical_text
        OR NEW.text_sha256 IS DISTINCT FROM OLD.text_sha256
        OR NEW.byte_sha256 IS DISTINCT FROM OLD.byte_sha256) THEN
        RAISE EXCEPTION 'document version text is immutable';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER document_versions_immutable BEFORE UPDATE ON document_versions
    FOR EACH ROW EXECUTE FUNCTION document_version_guard();

CREATE TABLE document_generations (
    tenant_id     uuid NOT NULL,
    document_id   uuid NOT NULL,
    generation    integer NOT NULL CHECK (generation >= 1),
    version       integer NOT NULL,
    status        text NOT NULL CHECK (status IN ('staging', 'active', 'retired', 'failed')),
    chunker       text NOT NULL,
    embed_model   text NOT NULL,
    collection    text NOT NULL,
    chunk_count   integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    activated_at  timestamptz,
    PRIMARY KEY (tenant_id, document_id, generation),
    FOREIGN KEY (tenant_id, document_id, version)
        REFERENCES document_versions (tenant_id, document_id, version) ON DELETE CASCADE
);
CREATE UNIQUE INDEX document_generations_one_active ON document_generations (tenant_id, document_id)
    WHERE status = 'active';

CREATE TABLE document_chunks (
    tenant_id       uuid NOT NULL,
    point_id        uuid NOT NULL,
    document_id     uuid NOT NULL,
    version         integer NOT NULL,
    generation      integer NOT NULL,
    ordinal         integer NOT NULL CHECK (ordinal >= 0),
    text            text NOT NULL,
    prefix          text NOT NULL DEFAULT '',
    char_start      integer NOT NULL CHECK (char_start >= 0),
    char_end        integer NOT NULL,
    page            integer,
    heading_path    text[] NOT NULL DEFAULT '{}',
    element_type    text NOT NULL,
    content_sha256  text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, point_id),
    UNIQUE (tenant_id, document_id, generation, ordinal),
    CHECK (char_end > char_start),
    FOREIGN KEY (tenant_id, document_id, generation)
        REFERENCES document_generations (tenant_id, document_id, generation) ON DELETE CASCADE
);

CREATE TABLE ingestion_jobs (
    id                  uuid PRIMARY KEY,
    tenant_id           uuid NOT NULL,
    document_id         uuid NOT NULL,
    version             integer NOT NULL,
    generation          integer,
    kind                text NOT NULL CHECK (kind IN ('ingest', 'reindex')),
    status              text NOT NULL CHECK (status IN ('pending', 'claimed', 'done')),
    attempts            integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    claimed_until       timestamptz,
    last_dispatched_at  timestamptz,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, document_id, version)
        REFERENCES document_versions (tenant_id, document_id, version) ON DELETE CASCADE
);

-- Exact spans an answer cited, bound to the document version and its text hash.
CREATE TABLE answer_sources (
    tenant_id     uuid NOT NULL,
    run_id        uuid NOT NULL,
    citation_id   text NOT NULL CHECK (citation_id ~ '^c[0-9]{1,3}$'),
    document_id   uuid NOT NULL,
    version       integer NOT NULL,
    generation    integer NOT NULL,
    point_id      uuid NOT NULL,
    char_start    integer NOT NULL CHECK (char_start >= 0),
    char_end      integer NOT NULL,
    text_sha256   text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    quote_sha256  text NOT NULL CHECK (quote_sha256 ~ '^[0-9a-f]{64}$'),
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, citation_id),
    CHECK (char_end > char_start),
    FOREIGN KEY (tenant_id, run_id) REFERENCES report_runs (tenant_id, id) ON DELETE CASCADE
);
CREATE TRIGGER answer_sources_immutable BEFORE UPDATE ON answer_sources
    FOR EACH ROW EXECUTE FUNCTION report_reject_mutation();

CREATE FUNCTION ingestion_jobs_due(p_limit integer, p_redispatch_after interval)
    RETURNS TABLE (job_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT j.id, j.tenant_id FROM public.ingestion_jobs j
     WHERE (j.status = 'pending'
            AND (j.last_dispatched_at IS NULL
                 OR j.last_dispatched_at < clock_timestamp() - p_redispatch_after))
        OR (j.status = 'claimed' AND j.claimed_until < clock_timestamp())
     ORDER BY j.created_at
     LIMIT least(greatest(p_limit, 0), 100)
$$;
REVOKE ALL ON FUNCTION ingestion_jobs_due(integer, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION document_version_guard() FROM PUBLIC;
"""


def _grants(app: str) -> str:
    return f"""
GRANT UPDATE (permission_state) ON resources TO {app};
GRANT UPDATE (kind) ON report_runs TO {app};
GRANT SELECT, INSERT ON documents TO {app};
GRANT UPDATE (title, current_version, active_generation, updated_at, deleted_at) ON documents TO {app};
GRANT SELECT, INSERT ON document_versions TO {app};
GRANT UPDATE (status, error_code, error_message, canonical_text, text_sha256, parser, parse_notes,
              updated_at) ON document_versions TO {app};
GRANT SELECT, INSERT, DELETE ON document_generations TO {app};
GRANT UPDATE (status, chunk_count, activated_at) ON document_generations TO {app};
GRANT SELECT, INSERT, DELETE ON document_chunks TO {app};
GRANT SELECT, INSERT ON ingestion_jobs TO {app};
GRANT UPDATE (generation, status, attempts, claimed_until, last_dispatched_at, updated_at)
    ON ingestion_jobs TO {app};
GRANT SELECT, INSERT ON answer_sources TO {app};
GRANT EXECUTE ON FUNCTION ingestion_jobs_due(integer, interval) TO {app};
"""


def _run(sql: str) -> None:
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(SCHEMA)
    for table in NEW_TENANT_TABLES:
        _run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _run(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
    _run(_grants(app))


def downgrade() -> None:
    _run(
        """
        DROP TABLE IF EXISTS answer_sources, ingestion_jobs, document_chunks, document_generations,
            document_versions, documents CASCADE;
        DROP FUNCTION IF EXISTS ingestion_jobs_due(integer, interval);
        DROP FUNCTION IF EXISTS document_version_guard();
        ALTER TABLE report_runs DROP COLUMN IF EXISTS kind;
        DROP TRIGGER resources_policy_epoch ON resources;
        CREATE TRIGGER resources_policy_epoch
            AFTER INSERT OR UPDATE OF classification, department, department_restricted,
                allowed_countries, status OR DELETE ON resources
            FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();
        ALTER TABLE resources DROP COLUMN IF EXISTS permission_state;
        """
    )
