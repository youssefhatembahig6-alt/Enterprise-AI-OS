"""Database engines and transaction-scoped tenant context.

The application role is subject to Row-Level Security. Every tenant-scoped
transaction starts by setting ``app.tenant_id`` / ``app.user_id`` with
``set_config(..., is_local => true)`` so the values disappear at COMMIT or
ROLLBACK. When a pooled connection is returned it is additionally rolled back
and cleaned with ``DISCARD ALL`` so nothing session-level can leak to the next
borrower. A transaction that never sets the context sees no tenant rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, event, text


def _discard_on_checkin(dbapi_connection: Any, _record: Any) -> None:
    if dbapi_connection is None:
        return
    dbapi_connection.rollback()
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("DISCARD ALL")
    finally:
        dbapi_connection.autocommit = previous


def create_app_engine(url: str, pool_size: int = 5, application_name: str = "eaios-app") -> Engine:
    engine = create_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        connect_args={
            "connect_timeout": 3,
            "application_name": application_name,
            # DISCARD ALL deallocates prepared statements, so psycopg must not
            # auto-prepare server-side statements on pooled connections.
            "prepare_threshold": None,
        },
    )
    event.listen(engine, "checkin", _discard_on_checkin)
    return engine


_SET_CONTEXT = text(
    "SELECT set_config('app.tenant_id', :tenant_id, true),"
    " set_config('app.user_id', :user_id, true)"
)


@contextmanager
def tenant_transaction(
    engine: Engine, tenant_id: UUID, user_id: UUID | None = None, *, snapshot: bool = False
) -> Iterator[Connection]:
    """Open a transaction bound to a trusted tenant (and optionally user) identity.

    ``tenant_id`` must come from a verified source (a validated session token or
    the restricted login lookup), never from request JSON or headers.

    ``snapshot=True`` runs the transaction at REPEATABLE READ, so every statement in it
    sees one snapshot. Use it for reads that combine several statements into one answer
    (for example a document's ``active_generation`` and its version ``status``): under the
    default READ COMMITTED a concurrent commit could land between them.
    """

    with engine.begin() as conn:
        if snapshot:  # must be the first statement of the transaction
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        conn.execute(
            _SET_CONTEXT,
            {"tenant_id": str(tenant_id), "user_id": str(user_id) if user_id else ""},
        )
        yield conn


@contextmanager
def contextless_transaction(engine: Engine) -> Iterator[Connection]:
    """A transaction with no tenant context: tenant tables return no rows.

    Used only for the restricted login lookup function and health checks.
    """

    with engine.begin() as conn:
        yield conn
