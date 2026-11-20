"""Database role names used by migrations (validated identifiers)."""

from __future__ import annotations

import os
import re

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _role(env: str, default: str) -> str:
    name = os.environ.get(env, default)
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{env} must be a lowercase SQL identifier")
    return name


def app_role_name() -> str:
    return _role("EAIOS_APP_DB_ROLE", "eaios_app")


def report_role_name() -> str:
    """Separate read-only role that executes validated report SQL (M2).

    It is created outside migrations by a superuser (``infra/postgres/init-roles.sh``,
    ``scripts/dev.py add-report-role`` or the test harness) because the owner role
    has NOCREATEROLE. Migrations only grant it the reporting surface.
    """

    return _role("EAIOS_REPORT_DB_ROLE", "eaios_report")


# Role-level defaults for the report query role, applied by a superuser. They are a
# second layer: the executor also sets these per transaction with SET LOCAL.
REPORT_ROLE_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("default_transaction_read_only", "on"),
    ("statement_timeout", "5000"),
    ("lock_timeout", "1000"),
    ("idle_in_transaction_session_timeout", "15000"),
    ("search_path", "reporting"),
    ("work_mem", "16MB"),
    ("temp_file_limit", "32MB"),
)


def report_role_settings_sql(role: str) -> list[str]:
    if not _IDENTIFIER.match(role):
        raise ValueError("report role must be a lowercase SQL identifier")
    return [f"ALTER ROLE {role} SET {name} = '{value}'" for name, value in REPORT_ROLE_DEFAULTS]
