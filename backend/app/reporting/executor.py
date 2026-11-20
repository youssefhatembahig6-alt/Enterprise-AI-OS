"""Executes validated report SQL through the separate read-only query role.

Per query:

1. Check out a query-role connection and read its backend PID.
2. In an *application-role* transaction bound to the trusted tenant and user, call
   ``report_context_open(pid, run, ttl)``. Only the application role can do this, and
   the function takes the tenant from the application transaction, never from SQL.
3. On the query connection: a READ ONLY transaction with local statement/lock/idle
   timeouts and ``search_path = reporting``; confirm ``report_context_tenant()``
   equals the expected tenant; run the deparsed validated statement under an outer
   ``LIMIT`` and fetch at most ``max_rows + 1`` rows; roll back.
4. Always close the context row, and return the connection to the pool, where it is
   rolled back and cleaned with ``DISCARD ALL``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import Engine, text

from app.db.engine import create_app_engine, tenant_transaction

# PostgreSQL type OIDs accepted in report results, mapped to evidence types.
RESULT_TYPES = {
    20: "integer",  # int8
    23: "integer",  # int4
    21: "integer",  # int2
    1700: "decimal",  # numeric
    1082: "date",
    25: "text",
    1043: "text",
    16: "boolean",
    2950: "text",  # uuid, returned as its canonical string
}


class QueryError(Exception):
    """Typed execution failure. ``code`` is safe to log and to show the model."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class QueryResult:
    executed_sql: str
    parameters: dict[str, Any]
    columns: list[dict[str, str]]
    rows: list[list[Any]]  # JSON-safe typed cells: Decimal -> exact string
    executed_at: datetime

    def row_dicts(self) -> list[dict[str, Any]]:
        names = [c["name"] for c in self.columns]
        return [dict(zip(names, row, strict=True)) for row in self.rows]


def create_report_engine(url: str, pool_size: int = 2) -> Engine:
    return create_app_engine(url, pool_size=pool_size, application_name="eaios-report")


def _cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise QueryError("non_finite_number")
        return format(value, "f")
    if isinstance(value, bool | int | str) or value is None:
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise QueryError("unsupported_result_type", type(value).__name__)


def result_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ReportExecutor:
    def __init__(
        self,
        app_engine: Engine,
        report_engine: Engine,
        *,
        statement_timeout_ms: int = 5000,
        max_rows: int = 200,
        context_ttl_seconds: int = 30,
    ) -> None:
        self._app = app_engine
        self._report = report_engine
        self._timeout_ms = int(statement_timeout_ms)
        self._max_rows = int(max_rows)
        self._ttl = int(context_ttl_seconds)

    @contextmanager
    def _bound_connection(
        self, tenant_id: UUID, user_id: UUID, run_id: UUID
    ) -> Iterator[psycopg.Connection[Any]]:
        conn = self._report.connect()
        pid: int | None = None
        try:
            raw = conn.connection.driver_connection
            if not isinstance(raw, psycopg.Connection):
                raise QueryError("unexpected_driver")
            raw.rollback()
            with raw.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                row = cur.fetchone()
                if row is None:
                    raise QueryError("no_backend_pid")
                pid = int(row[0])
            raw.rollback()
            with tenant_transaction(self._app, tenant_id, user_id) as app_conn:
                app_conn.execute(
                    text("SELECT report_context_open(:pid, :run, :ttl)"),
                    {"pid": pid, "run": str(run_id), "ttl": self._ttl},
                )
            yield raw
        finally:
            try:
                if pid is not None:
                    with tenant_transaction(self._app, tenant_id, user_id) as app_conn:
                        app_conn.execute(text("SELECT report_context_close(:pid)"), {"pid": pid})
            finally:
                conn.close()

    def execute(self, *, tenant_id: UUID, user_id: UUID, run_id: UUID, sql: str) -> QueryResult:
        """Run one validated, deparsed SELECT and return exact typed rows."""

        limit = self._max_rows + 1
        # Server-side bound parameter; literal % in the validated SQL is escaped.
        # ``sql`` is the deparsed tree that passed validate_sql(); only the limit is bound.
        wrapped = f"SELECT * FROM ({sql.replace('%', '%%')}) AS report_query LIMIT %(row_limit)s"  # noqa: S608
        parameters = {"row_limit": limit}
        with self._bound_connection(tenant_id, user_id, run_id) as raw:
            try:
                with raw.cursor() as cur:
                    # psycopg opens the transaction implicitly; these run first in it.
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(f"SET LOCAL statement_timeout = {self._timeout_ms}")
                    cur.execute("SET LOCAL lock_timeout = 1000")
                    cur.execute("SET LOCAL idle_in_transaction_session_timeout = 15000")
                    cur.execute("SET LOCAL search_path = reporting")
                    cur.execute("SET LOCAL work_mem = '16MB'")
                    cur.execute("SELECT reporting_ctx.report_context_tenant()")
                    bound = cur.fetchone()
                    if bound is None or bound[0] is None or UUID(str(bound[0])) != tenant_id:
                        raise QueryError("context_mismatch")
                    cur.execute(wrapped, parameters)
                    if cur.description is None:
                        raise QueryError("no_result_set")
                    columns = []
                    for column in cur.description:
                        kind = RESULT_TYPES.get(column.type_code)
                        if kind is None:
                            raise QueryError("unsupported_result_type", str(column.type_code))
                        columns.append({"name": column.name, "type": kind})
                    fetched = cur.fetchmany(limit)
                    executed_at = datetime.now(UTC)
            except psycopg.errors.QueryCanceled as exc:
                raise QueryError("statement_timeout") from exc
            except psycopg.errors.InsufficientPrivilege as exc:
                raise QueryError("permission_denied") from exc
            except psycopg.errors.ReadOnlySqlTransaction as exc:
                raise QueryError("read_only_violation") from exc
            except psycopg.Error as exc:
                raise QueryError("database_error", exc.sqlstate or type(exc).__name__) from exc
            finally:
                raw.rollback()
        if len(fetched) > self._max_rows:
            raise QueryError("too_many_rows", f"limit {self._max_rows}")
        rows = [[_cell(value) for value in row] for row in fetched]
        return QueryResult(
            executed_sql=sql,
            parameters=parameters,
            columns=columns,
            rows=rows,
            executed_at=executed_at,
        )
