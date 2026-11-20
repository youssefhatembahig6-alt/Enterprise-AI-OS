"""M0 foundation: tenants, identity, roles/capabilities, groups, resources, grants,
sessions, audit events, Row-Level Security and the restricted login lookup.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-11-20
"""

from alembic import op

from app.db.roles import app_role_name

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None

# Every tenant-owned table. Each has tenant_id NOT NULL, RLS enabled and a
# tenant_isolation policy. audit_events is handled separately (insert-only).
TENANT_TABLES = (
    "users",
    "roles",
    "role_capabilities",
    "user_roles",
    "groups",
    "group_memberships",
    "resources",
    "resource_grants",
    "sessions",
)

# Tables whose changes alter authorization results and therefore bump the
# tenant policy epoch. Column lists restrict UPDATE triggers to relevant columns.
EPOCH_TRIGGERS = {
    "users": "status, clearance, country, department",
    "role_capabilities": None,
    "user_roles": None,
    "group_memberships": None,
    "resources": "classification, department, department_restricted, allowed_countries, status",
    "resource_grants": None,
}

SCHEMA = r"""
CREATE FUNCTION app_current_tenant_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

CREATE TABLE tenants (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug          text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name          text NOT NULL,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    policy_epoch  bigint NOT NULL DEFAULT 1 CHECK (policy_epoch >= 1),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    email          text NOT NULL CHECK (email = lower(email) AND position('@' in email) > 1),
    display_name   text NOT NULL,
    password_hash  text NOT NULL,
    status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    clearance      text CHECK (clearance IN ('public', 'internal', 'confidential', 'restricted')),
    country        text CHECK (country ~ '^[A-Z]{2}$'),
    department     text CHECK (department ~ '^[a-z][a-z0-9_-]{0,62}$'),
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, email)
);

CREATE TABLE roles (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    key        text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,62}$'),
    name       text NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, key)
);

CREATE TABLE role_capabilities (
    tenant_id   uuid NOT NULL,
    role_id     uuid NOT NULL,
    capability  text NOT NULL CHECK (capability IN ('resource.read', 'resource.admin')),
    PRIMARY KEY (role_id, capability),
    FOREIGN KEY (tenant_id, role_id) REFERENCES roles (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE user_roles (
    tenant_id         uuid NOT NULL,
    user_id           uuid NOT NULL,
    role_id           uuid NOT NULL,
    scope_department  text CHECK (scope_department ~ '^[a-z][a-z0-9_-]{0,62}$'),
    UNIQUE NULLS NOT DISTINCT (user_id, role_id, scope_department),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, role_id) REFERENCES roles (tenant_id, id) ON DELETE CASCADE
);

CREATE TABLE groups (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    key        text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_-]{0,62}$'),
    name       text NOT NULL,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, key)
);

CREATE TABLE group_memberships (
    tenant_id  uuid NOT NULL,
    group_id   uuid NOT NULL,
    user_id    uuid NOT NULL,
    PRIMARY KEY (group_id, user_id),
    FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
);

-- Generic protected resource. Documents, datasets and repositories reference
-- resources(tenant_id, id) in later milestones; M0 stores metadata only.
CREATE TABLE resources (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    kind                   text NOT NULL CHECK (kind IN ('document')),
    external_ref           text NOT NULL,
    title                  text NOT NULL,
    classification         text NOT NULL
                           CHECK (classification IN ('public', 'internal', 'confidential', 'restricted')),
    department             text NOT NULL CHECK (department ~ '^[a-z][a-z0-9_-]{0,62}$'),
    department_restricted  boolean NOT NULL,
    allowed_countries      text[] NOT NULL CHECK (cardinality(allowed_countries) >= 1),
    status                 text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, kind, external_ref)
);

CREATE TABLE resource_grants (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL,
    resource_id  uuid NOT NULL,
    user_id      uuid,
    group_id     uuid,
    effect       text NOT NULL CHECK (effect IN ('allow', 'deny')),
    action       text NOT NULL CHECK (action IN ('resource.read')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(user_id, group_id) = 1),
    UNIQUE NULLS NOT DISTINCT (resource_id, user_id, group_id, effect, action),
    FOREIGN KEY (tenant_id, resource_id) REFERENCES resources (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, group_id) REFERENCES groups (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX resource_grants_resource_idx ON resource_grants (tenant_id, resource_id);

CREATE TABLE sessions (
    id          uuid PRIMARY KEY,
    tenant_id   uuid NOT NULL,
    user_id     uuid NOT NULL,
    issued_at   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    CHECK (expires_at > issued_at),
    FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

-- Audit events. tenant_id/user_id are NULL when not yet known (for example a
-- login attempt against an unknown tenant). The application role may only
-- INSERT; reading is reserved for a future scoped admin path.
CREATE TABLE audit_events (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    request_id   text,
    tenant_id    uuid REFERENCES tenants (id) ON DELETE CASCADE,
    user_id      uuid,
    event_type   text NOT NULL,
    action       text NOT NULL,
    resource_id  uuid,
    decision     text NOT NULL CHECK (decision IN ('allow', 'deny', 'info')),
    reason       text NOT NULL,
    details      jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_events_tenant_time_idx ON audit_events (tenant_id, occurred_at);

-- Policy epoch: tenant-wide, monotonic, raised whenever an authorization input changes.
CREATE FUNCTION bump_policy_epoch() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
DECLARE
    target uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target := OLD.tenant_id;
    ELSE
        target := NEW.tenant_id;
    END IF;
    UPDATE public.tenants SET policy_epoch = policy_epoch + 1 WHERE id = target;
    RETURN NULL;
END
$$;

-- Restricted login lookup. Runs as the table owner so it can find a user before
-- a tenant context exists, but only returns the single row matching an exact
-- tenant slug and normalized email, with the minimum columns needed to verify
-- a password. It never enumerates tenants or users.
CREATE FUNCTION auth_lookup_login(p_tenant_slug text, p_email text)
    RETURNS TABLE (tenant_id uuid, tenant_status text, user_id uuid, user_status text,
                   password_hash text)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    SELECT t.id, t.status, u.id, u.status, u.password_hash
      FROM public.tenants t
      JOIN public.users u ON u.tenant_id = t.id
     WHERE t.slug = p_tenant_slug
       AND u.email = lower(p_email)
     LIMIT 1
$$;

-- Maintenance used by the scheduler: removes sessions expired more than a day ago.
CREATE FUNCTION purge_expired_sessions() RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
DECLARE
    removed integer;
BEGIN
    DELETE FROM public.sessions WHERE expires_at < now() - interval '1 day';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END
$$;

REVOKE ALL ON FUNCTION bump_policy_epoch() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_lookup_login(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION purge_expired_sessions() FROM PUBLIC;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants
    USING (id = app_current_tenant_id());

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_insert ON audit_events FOR INSERT
    WITH CHECK (tenant_id IS NULL OR tenant_id = app_current_tenant_id());
"""


def _grants(app: str) -> str:
    return f"""
GRANT EXECUTE ON FUNCTION app_current_tenant_id() TO {app};
GRANT EXECUTE ON FUNCTION auth_lookup_login(text, text) TO {app};
GRANT EXECUTE ON FUNCTION purge_expired_sessions() TO {app};

GRANT SELECT ON alembic_version TO {app};
GRANT SELECT (id, slug, name, status, policy_epoch) ON tenants TO {app};

-- password_hash is writable on insert but never readable by the application role.
GRANT SELECT (id, tenant_id, email, display_name, status, clearance, country, department,
              created_at, updated_at) ON users TO {app};
GRANT INSERT ON users TO {app};
GRANT UPDATE (display_name, status, clearance, country, department, updated_at) ON users TO {app};

GRANT SELECT, INSERT, DELETE ON roles, role_capabilities, user_roles, groups,
      group_memberships, resource_grants TO {app};
GRANT UPDATE (name) ON roles, groups TO {app};
GRANT UPDATE (scope_department) ON user_roles TO {app};

GRANT SELECT, INSERT ON resources TO {app};
GRANT UPDATE (title, classification, department, department_restricted, allowed_countries,
              status, updated_at) ON resources TO {app};

GRANT SELECT, INSERT ON sessions TO {app};
GRANT UPDATE (revoked_at) ON sessions TO {app};

GRANT INSERT ON audit_events TO {app};
"""


def _run(sql: str) -> None:
    # Multi-statement DDL: send without parameters so the driver uses the simple
    # query protocol and SQLAlchemy does not interpret colons as bind markers.
    op.get_bind().execution_options(no_parameters=True).exec_driver_sql(sql)


def upgrade() -> None:
    app = app_role_name()
    _run(SCHEMA)
    for table in TENANT_TABLES:
        _run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _run(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant_id())"
            " WITH CHECK (tenant_id = app_current_tenant_id())"
        )
    for table, columns in EPOCH_TRIGGERS.items():
        update = f"UPDATE OF {columns}" if columns else "UPDATE"
        _run(
            f"CREATE TRIGGER {table}_policy_epoch AFTER INSERT OR {update} OR DELETE ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION bump_policy_epoch()"
        )
    _run(_grants(app))


def downgrade() -> None:
    _run(
        """
        DROP TABLE IF EXISTS audit_events, sessions, resource_grants, resources,
            group_memberships, groups, user_roles, role_capabilities, roles, users, tenants
            CASCADE;
        DROP FUNCTION IF EXISTS purge_expired_sessions();
        DROP FUNCTION IF EXISTS auth_lookup_login(text, text);
        DROP FUNCTION IF EXISTS bump_policy_epoch();
        DROP FUNCTION IF EXISTS app_current_tenant_id();
        """
    )
