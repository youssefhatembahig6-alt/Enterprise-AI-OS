"""M5: legal and code packs, GitHub repository sources, desktop local-folder sources.

Invariants (docs/adr/0501..0503):

* **Vocabularies.** Packs gain ``legal`` and ``code``; connectors gain ``github`` and
  ``local_folder``; run kinds gain ``legal`` and ``code``; documents may come from
  ``github`` and ``local_folder`` sources.
* **GitHub permissions are repository-level and as current as the last verified sync**
  (same freshness rule as Drive). ``github_collaborators`` holds the read-capable logins
  read in the last successful sync. A login becomes an allow only while it is linked to
  a tenant user in ``external_accounts``; unlinking takes effect at the next
  authorization point. Repository visibility never widens access.
* **Local folders are owned by the backend's choice, not the operating system's.** A
  ``local_folder`` source has an owner; the owner and the groups in ``source_grants``
  are its only readers (layer 4). There is no external permission system to go stale,
  so freshness does not apply; the source must still be active and the connector
  enabled.
* ``connector_items`` records every path a GitHub or local-folder source has seen,
  including excluded, oversized and symlinked entries (never ingested). Removed items
  keep a row and their document keeps its tombstone.
* ``local_folder_events`` makes desktop event batches idempotent: a replayed event id
  (offline retry, duplicate watcher event) is recognised and not applied twice.
* A deleted connector source keeps its row, and the same root can be added again
  (uniqueness applies to non-deleted sources only).
* ``document_versions.source_revision`` records, immutably, where a version came from:
  repository, commit, path and blob for GitHub; relative path and content hash for a
  local folder. Code citations read it.

Revision ID: 0011_m5_wave2
Revises: 0010_drive_group_grant_key
Create Date: 2026-11-21
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0011_m5_wave2"
down_revision = "0010_drive_group_grant_key"
branch_labels = None
depends_on = None

NEW_TENANT_TABLES = (
    "connector_items",
    "source_grants",
    "external_accounts",
    "github_collaborators",
    "local_folder_events",
)

SCHEMA = r"""
ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
    CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports', 'legal', 'code']::text[]);
ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_connectors_check;
ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_connectors_check
    CHECK (enabled_connectors
           <@ ARRAY['upload', 'google_drive', 'github', 'local_folder']::text[]);

ALTER TABLE report_runs DROP CONSTRAINT report_runs_kind_check;
ALTER TABLE report_runs ADD CONSTRAINT report_runs_kind_check
    CHECK (kind IN ('report', 'documents', 'legal', 'code'));

ALTER TABLE documents DROP CONSTRAINT documents_source_check;
ALTER TABLE documents ADD CONSTRAINT documents_source_check
    CHECK (source IN ('upload', 'fixture', 'google_drive', 'github', 'local_folder'));

ALTER TABLE document_versions ADD COLUMN source_revision jsonb;

ALTER TABLE connector_sources DROP CONSTRAINT connector_sources_kind_check;
ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_kind_check
    CHECK (kind IN ('google_drive', 'github', 'local_folder'));
ALTER TABLE connector_sources DROP CONSTRAINT connector_sources_external_root_check;
ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_external_root_check CHECK (
    (kind = 'google_drive' AND external_root ~ '^[A-Za-z0-9_-]{5,200}$')
    OR (kind = 'github'
        AND external_root ~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$')
    OR (kind = 'local_folder'
        AND external_root ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'));
-- GitHub: the branch synced. Local folder: the device label shown to the owner.
ALTER TABLE connector_sources ADD COLUMN external_ref text
    CHECK (external_ref IS NULL OR char_length(external_ref) BETWEEN 1 AND 200);
ALTER TABLE connector_sources ADD COLUMN owner_user_id uuid;
ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_owner_fk
    FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users (tenant_id, id);
ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_owner_check
    CHECK ((kind = 'local_folder') = (owner_user_id IS NOT NULL));
ALTER TABLE connector_sources ADD COLUMN visibility text
    CHECK (visibility IS NULL OR visibility IN ('private', 'internal', 'public'));
ALTER TABLE connector_sources ADD COLUMN external_revision text
    CHECK (external_revision IS NULL OR external_revision ~ '^[0-9a-f]{40}$');
ALTER TABLE connector_sources ADD COLUMN unresolved_principals jsonb NOT NULL DEFAULT '[]'::jsonb;

-- A deleted source keeps its row (audit trail, tombstoned documents); the same folder or
-- repository can be added again as a new source.
ALTER TABLE connector_sources DROP CONSTRAINT connector_sources_tenant_id_kind_external_root_key;
CREATE UNIQUE INDEX connector_sources_live_root_idx
    ON connector_sources (tenant_id, kind, external_root) WHERE status <> 'deleted';

CREATE TABLE connector_items (
    tenant_id       uuid NOT NULL,
    source_id       uuid NOT NULL,
    path            text NOT NULL CHECK (char_length(path) BETWEEN 1 AND 1024
                                         AND path !~ '(^/|^\.\./|/\.\./|/\.\.$|^\.\.$|\\)'),
    document_id     uuid,
    size            bigint CHECK (size IS NULL OR size >= 0),
    content_sha256  text CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    external_sha    text CHECK (external_sha IS NULL OR external_sha ~ '^[0-9a-f]{40}$'),
    revision        text CHECK (revision IS NULL OR char_length(revision) <= 100),
    state           text NOT NULL CHECK (state IN
                    ('active', 'removed', 'unsupported', 'excluded', 'too_large', 'symlink')),
    reason          text,
    last_seq        bigint,
    last_seen_run   uuid,
    first_seen_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    removed_at      timestamptz,
    PRIMARY KEY (tenant_id, source_id, path),
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
);
CREATE UNIQUE INDEX connector_items_document_idx ON connector_items (tenant_id, document_id)
    WHERE document_id IS NOT NULL;

-- Backend-assigned readers of a local-folder source (besides its owner).
CREATE TABLE source_grants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL,
    source_id   uuid NOT NULL,
    user_id     uuid,
    group_id    uuid,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((user_id IS NULL) <> (group_id IS NULL)),
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX source_grants_user_idx ON source_grants (tenant_id, source_id, user_id)
    WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX source_grants_group_idx ON source_grants (tenant_id, source_id, group_id)
    WHERE group_id IS NOT NULL;

-- An external identity (GitHub login) linked to exactly one tenant user by an
-- identity administrator. GitHub logins are case-insensitive; stored lower-case.
CREATE TABLE external_accounts (
    tenant_id   uuid NOT NULL,
    provider    text NOT NULL CHECK (provider IN ('github')),
    login       text NOT NULL CHECK (login = lower(login)
                                     AND login ~ '^[a-z0-9][a-z0-9-]{0,38}$'),
    user_id     uuid NOT NULL,
    created_by  uuid,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider, login),
    UNIQUE (tenant_id, provider, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
);

-- Read-capable repository principals as of the last verified sync of the source.
CREATE TABLE github_collaborators (
    tenant_id   uuid NOT NULL,
    source_id   uuid NOT NULL,
    login       text NOT NULL CHECK (login = lower(login)),
    permission  text NOT NULL CHECK (permission IN ('pull', 'triage', 'push', 'maintain', 'admin')),
    PRIMARY KEY (tenant_id, source_id, login),
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE local_folder_events (
    tenant_id   uuid NOT NULL,
    source_id   uuid NOT NULL,
    event_id    uuid NOT NULL,
    path        text NOT NULL,
    op          text NOT NULL CHECK (op IN ('upsert', 'delete')),
    result      text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, source_id, event_id),
    FOREIGN KEY (tenant_id, source_id) REFERENCES connector_sources (tenant_id, id) ON DELETE CASCADE
);

CREATE TRIGGER source_grants_policy_epoch AFTER INSERT OR UPDATE OR DELETE ON source_grants
    FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();
CREATE TRIGGER external_accounts_policy_epoch AFTER INSERT OR UPDATE OR DELETE ON external_accounts
    FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();
CREATE TRIGGER github_collaborators_policy_epoch
    AFTER INSERT OR UPDATE OR DELETE ON github_collaborators
    FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch();

-- Local folders are pushed by the desktop app; only pull connectors are scheduled.
CREATE OR REPLACE FUNCTION connector_sources_due(p_limit integer)
    RETURNS TABLE (source_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT s.id, s.tenant_id
      FROM public.connector_sources s
      JOIN public.tenant_settings ts ON ts.tenant_id = s.tenant_id
      JOIN public.tenants t ON t.id = s.tenant_id AND t.status = 'active'
     WHERE s.status = 'active'
       AND s.kind IN ('google_drive', 'github')
       AND s.kind = ANY (ts.enabled_connectors)
       AND (s.sync_claimed_until IS NULL OR s.sync_claimed_until < clock_timestamp())
       AND (s.last_sync_started_at IS NULL
            OR (ts.sync_mode = 'demo'
                AND s.last_sync_started_at
                    < clock_timestamp() - make_interval(secs => ts.sync_interval_seconds))
            OR (ts.sync_mode = 'nightly'
                AND s.last_sync_started_at < (
                    CASE WHEN clock_timestamp() >= date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                                   AT TIME ZONE 'UTC' + interval '2 hours'
                         THEN date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC' + interval '2 hours'
                         ELSE date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC')
                                  AT TIME ZONE 'UTC' - interval '22 hours'
                    END)))
     ORDER BY s.last_sync_started_at NULLS FIRST
     LIMIT least(greatest(p_limit, 0), 50)
$$;
REVOKE ALL ON FUNCTION connector_sources_due(integer) FROM PUBLIC;
"""


def _grants(app: str) -> str:
    return f"""
GRANT SELECT, INSERT ON connector_items TO {app};
GRANT UPDATE (document_id, size, content_sha256, external_sha, revision, state, reason, last_seq,
              last_seen_run, updated_at, removed_at)
    ON connector_items TO {app};
GRANT SELECT, INSERT, DELETE ON source_grants TO {app};
GRANT SELECT, INSERT, DELETE ON external_accounts TO {app};
GRANT SELECT, INSERT, DELETE ON github_collaborators TO {app};
GRANT UPDATE (permission) ON github_collaborators TO {app};
GRANT SELECT, INSERT ON local_folder_events TO {app};
GRANT UPDATE (external_ref, visibility, external_revision, unresolved_principals)
    ON connector_sources TO {app};
GRANT EXECUTE ON FUNCTION connector_sources_due(integer) TO {app};
GRANT UPDATE (chunker) ON document_generations TO {app};
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
        DROP TABLE IF EXISTS local_folder_events, github_collaborators, external_accounts,
            source_grants, connector_items CASCADE;
        UPDATE documents SET deleted_at = coalesce(deleted_at, now())
         WHERE source IN ('github', 'local_folder');
        DELETE FROM connector_sources WHERE kind IN ('github', 'local_folder');
        DROP INDEX IF EXISTS connector_sources_live_root_idx;
        ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_tenant_id_kind_external_root_key
            UNIQUE (tenant_id, kind, external_root);
        ALTER TABLE connector_sources DROP CONSTRAINT IF EXISTS connector_sources_owner_check;
        ALTER TABLE connector_sources DROP CONSTRAINT IF EXISTS connector_sources_owner_fk;
        ALTER TABLE connector_sources DROP COLUMN IF EXISTS unresolved_principals,
            DROP COLUMN IF EXISTS external_revision, DROP COLUMN IF EXISTS visibility,
            DROP COLUMN IF EXISTS owner_user_id, DROP COLUMN IF EXISTS external_ref;
        ALTER TABLE connector_sources DROP CONSTRAINT connector_sources_external_root_check;
        ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_external_root_check
            CHECK (external_root ~ '^[A-Za-z0-9_-]{5,200}$');
        ALTER TABLE connector_sources DROP CONSTRAINT connector_sources_kind_check;
        ALTER TABLE connector_sources ADD CONSTRAINT connector_sources_kind_check
            CHECK (kind IN ('google_drive'));
        ALTER TABLE document_versions DROP COLUMN IF EXISTS source_revision;
        UPDATE tenant_settings SET
            enabled_packs = array(SELECT unnest(enabled_packs)
                                  INTERSECT SELECT unnest(ARRAY['documents', 'sales_reports'])),
            enabled_connectors = array(SELECT unnest(enabled_connectors)
                                       INTERSECT SELECT unnest(ARRAY['upload', 'google_drive']));
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_packs_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_packs_check
            CHECK (enabled_packs <@ ARRAY['documents', 'sales_reports']::text[]);
        ALTER TABLE tenant_settings DROP CONSTRAINT tenant_settings_enabled_connectors_check;
        ALTER TABLE tenant_settings ADD CONSTRAINT tenant_settings_enabled_connectors_check
            CHECK (enabled_connectors <@ ARRAY['upload', 'google_drive']::text[]);
        """
    )
