"""Restricted report SQL on real PostgreSQL.

Covers the query role's properties and privileges, database enforcement when the
application validator is bypassed entirely (raw SQL as the query role), tenant
isolation through the unforgeable per-backend context, timeouts and row limits,
connection-pool reuse across tenants, and evidence immutability.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, InternalError, ProgrammingError

from app.db.engine import create_app_engine, tenant_transaction
from app.reporting.executor import QueryError, ReportExecutor, create_report_engine
from app.reporting.sql_guard import validate_sql
from tests.integration.conftest import ADMIN_URL, DbHarness, owner_exec

Q1_SQL = validate_sql(
    "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales, sum(o.tax) AS tax_amount,"
    " sum(o.total) AS gross_sales FROM reporting.sales_orders o"
    " WHERE o.order_date >= DATE '2026-01-01' AND o.order_date <= DATE '2026-03-31'"
    " AND o.status = 'FULFILLED'"
).deparsed


@pytest.fixture(scope="module")
def app_engine_r(report_db: DbHarness) -> Iterator[Engine]:
    engine = create_app_engine(report_db.app_url, pool_size=2)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def report_engine(report_db: DbHarness) -> Iterator[Engine]:
    engine = create_report_engine(report_db.report_url, pool_size=1)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def executor(app_engine_r: Engine, report_engine: Engine) -> ReportExecutor:
    return ReportExecutor(app_engine_r, report_engine, statement_timeout_ms=2000, max_rows=50)


def ids(db: DbHarness, ref: str) -> tuple[uuid.UUID, uuid.UUID]:
    slug, key = ref.split("/", 1)
    tenant = db.foundation.tenants[slug]
    return tenant.id, tenant.users[key].id


def raw_report_connection(db: DbHarness) -> psycopg.Connection[Any]:
    url = make_url(db.report_url)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
        autocommit=True,
    )


# -- role properties ------------------------------------------------------------------


def test_query_role_properties(report_db: DbHarness) -> None:
    row = owner_exec(
        report_db,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit, rolconfig"
        " FROM pg_roles WHERE rolname = :r",
        {"r": report_db.report_role},
    ).one()
    assert (row.rolsuper, row.rolbypassrls, row.rolcreaterole, row.rolcreatedb) == (
        False,
        False,
        False,
        False,
    )
    assert row.rolinherit is False
    config = dict(item.split("=", 1) for item in row.rolconfig)
    assert config["default_transaction_read_only"] == "on"
    assert config["statement_timeout"] == "5000"
    assert config["search_path"] == "reporting"
    owned = owner_exec(
        report_db,
        "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = :r",
        {"r": report_db.report_role},
    ).scalar_one()
    assert owned == 0


def test_query_role_privileges_are_limited_to_reporting(report_db: DbHarness) -> None:
    role = report_db.report_role

    def check(sql: str, **params: Any) -> bool:
        return bool(owner_exec(report_db, sql, {"r": role, **params}).scalar_one())

    assert check("SELECT has_schema_privilege(:r, 'reporting', 'USAGE')")
    assert not check("SELECT has_schema_privilege(:r, 'public', 'USAGE')")
    assert not check("SELECT has_schema_privilege(:r, 'reporting', 'CREATE')")
    for view in (
        "sales_orders",
        "sales_order_lines",
        "sales_invoices",
        "sales_products",
        "sales_customers",
    ):
        assert check("SELECT has_table_privilege(:r, :t, 'SELECT')", t=f"reporting.{view}")
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not check(
                "SELECT has_table_privilege(:r, :t, :p)", t=f"reporting.{view}", p=privilege
            )
    for table in (
        "users",
        "sessions",
        "resource_grants",
        "audit_events",
        "report_evidence",
        "report_query_contexts",
        "business_dataset_imports",
        "tenants",
    ):
        assert not check("SELECT has_table_privilege(:r, :t, 'SELECT')", t=f"public.{table}")
    # Base sales tables: only the columns the views expose; never tenant_id.
    assert not check(
        "SELECT has_column_privilege(:r, 'public.sales_orders', 'tenant_id', 'SELECT')"
    )
    assert not check(
        "SELECT has_column_privilege(:r, 'public.sales_orders', 'sales_rep_source_id', 'SELECT')"
    )
    assert not check(
        "SELECT has_function_privilege(:r,"
        " 'public.report_context_open(integer, timestamptz, text, uuid, integer)', 'EXECUTE')"
    )


# -- database enforcement without the validator ---------------------------------------


def test_raw_query_role_without_context_sees_no_rows(report_db: DbHarness) -> None:
    niletech, _ = ids(report_db, "niletech/finance-reader")
    with raw_report_connection(report_db) as conn:
        assert conn.execute("SELECT count(*) FROM reporting.sales_orders").fetchone() == (0,)
        assert conn.execute("SELECT reporting_ctx.report_context_tenant()").fetchone() == (None,)
        # Forging the application's session variable does not widen access.
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(niletech),))
        conn.execute(f"SET app.user_id = '{uuid.uuid4()}'")
        assert conn.execute("SELECT count(*) FROM reporting.sales_orders").fetchone() == (0,)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM public.users",
        "SELECT * FROM public.sales_orders",
        "SELECT tenant_id FROM sales_orders",
        "SELECT * FROM public.report_query_contexts",
        "SELECT * FROM public.report_evidence",
        "SELECT public.report_context_open(pg_backend_pid(), now(), repeat('0', 64),"
        " gen_random_uuid(), 60)",
        "INSERT INTO reporting.sales_orders (id) VALUES (gen_random_uuid())",
        "UPDATE reporting.sales_orders SET subtotal = 0",
        "DELETE FROM reporting.sales_orders",
        "CREATE TABLE reporting.stolen (x int)",
        "CREATE TEMP TABLE stolen AS SELECT 1",
        "SET ROLE " + "eaios_owner",
    ],
)
def test_raw_query_role_cannot_escape(report_db: DbHarness, sql: str) -> None:
    with raw_report_connection(report_db) as conn, pytest.raises(psycopg.Error):
        conn.execute(sql)


def test_query_role_is_read_only_by_default(report_db: DbHarness) -> None:
    with raw_report_connection(report_db) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW search_path").fetchone() == ("reporting",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TEMP TABLE t (x int)")


def session_identity(conn: psycopg.Connection[Any]) -> tuple[int, Any]:
    row = conn.execute(
        "SELECT pg_backend_pid(), a.backend_start FROM pg_stat_get_activity(pg_backend_pid()) a"
    ).fetchone()
    assert row is not None
    return int(row[0]), row[1]


def open_context(
    engine: Engine, db: DbHarness, pid: int, start: Any, secret: str, ttl: int = 30
) -> None:
    tenant, user = ids(db, "niletech/finance-reader")
    with tenant_transaction(engine, tenant, user) as conn:
        conn.execute(
            text("SELECT report_context_open(:p, :s, :d, :r, :t)"),
            {
                "p": pid,
                "s": start,
                "d": hashlib.sha256(secret.encode()).hexdigest(),
                "r": str(uuid.uuid4()),
                "t": ttl,
            },
        )


def close_context(engine: Engine, db: DbHarness, pid: int) -> None:
    tenant, user = ids(db, "niletech/finance-reader")
    with tenant_transaction(engine, tenant, user) as conn:
        conn.execute(text("SELECT report_context_close(:p)"), {"p": pid})


def count_with_secret(conn: psycopg.Connection[Any], secret: str | None) -> int:
    """Count visible orders in one transaction, optionally holding the secret."""

    with conn.transaction():
        if secret is not None:
            conn.execute("SELECT set_config('eaios.report_ctx', %s, true)", (secret,))
        row = conn.execute("SELECT count(*) FROM reporting.sales_orders").fetchone()
    assert row is not None
    return int(row[0])


def test_context_requires_session_identity_and_transaction_secret(
    report_db: DbHarness, app_engine_r: Engine
) -> None:
    secret = secrets.token_hex(32)
    with raw_report_connection(report_db) as bound, raw_report_connection(report_db) as other:
        pid, start = session_identity(bound)
        open_context(app_engine_r, report_db, pid, start, secret)
        try:
            assert count_with_secret(bound, secret) == 2400
            # The secret is transaction-local: the next transaction does not hold it.
            assert count_with_secret(bound, None) == 0
            assert count_with_secret(bound, secrets.token_hex(32)) == 0
            # Another session with the right secret still has the wrong pid and start.
            assert count_with_secret(other, secret) == 0
            # Calling the definer lookup directly with a claimed start needs the secret.
            row = other.execute(
                "SELECT reporting_ctx.report_context_lookup(%s, NULL)", (start,)
            ).fetchone()
            assert row == (None,)
        finally:
            close_context(app_engine_r, report_db, pid)
        assert count_with_secret(bound, secret) == 0


def test_stale_context_is_not_inherited_by_a_reused_pid(
    report_db: DbHarness, app_engine_r: Engine
) -> None:
    """Simulate PID reuse: a row left for this PID by an earlier session (different
    backend_start) is rejected even when the secret is known."""

    secret = secrets.token_hex(32)
    with raw_report_connection(report_db) as conn:
        pid, start = session_identity(conn)
        earlier_session = start - timedelta(minutes=5)
        open_context(app_engine_r, report_db, pid, earlier_session, secret)
        try:
            assert count_with_secret(conn, secret) == 0
            assert conn.execute("SELECT reporting_ctx.report_context_tenant()").fetchone() == (
                None,
            )
        finally:
            close_context(app_engine_r, report_db, pid)


def test_crashed_backend_context_is_cleaned_up(
    report_db: DbHarness, app_engine_r: Engine, executor: ReportExecutor
) -> None:
    secret = secrets.token_hex(32)
    crashed = raw_report_connection(report_db)
    pid, start = session_identity(crashed)
    open_context(app_engine_r, report_db, pid, start, secret, ttl=120)
    # Simulate a worker crash: the server kills the backend without a context close.
    admin = create_engine(ADMIN_URL or "", poolclass=pool.NullPool)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
    finally:
        admin.dispose()
    crashed.close()
    for _ in range(50):
        alive = owner_exec(
            report_db, "SELECT count(*) FROM pg_stat_activity WHERE pid = :p", {"p": pid}
        ).scalar_one()
        if not alive:
            break
        time.sleep(0.05)
    assert (
        owner_exec(
            report_db,
            "SELECT count(*) FROM report_query_contexts WHERE backend_pid = :p",
            {"p": pid},
        ).scalar_one()
        == 1
    )
    tenant, user = ids(report_db, "niletech/finance-reader")
    try:
        executor.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=Q1_SQL)
    finally:
        leftover = owner_exec(
            report_db,
            "SELECT count(*) FROM report_query_contexts WHERE backend_pid = :p",
            {"p": pid},
        ).scalar_one()
        owner_exec(
            report_db, "DELETE FROM report_query_contexts WHERE backend_pid = :p", {"p": pid}
        )
    # Opening any new context removed the dead backend's row.
    assert leftover == 0


def test_pooled_connection_with_lingering_row_sees_nothing(
    report_db: DbHarness, executor: ReportExecutor, report_engine: Engine, app_engine_r: Engine
) -> None:
    """Even if a context row for a pooled connection survived, the next borrower lacks
    the transaction secret and sees no rows."""

    tenant, user = ids(report_db, "niletech/finance-reader")
    executor.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=Q1_SQL)
    with report_engine.connect() as conn:
        raw = conn.connection.driver_connection
        assert isinstance(raw, psycopg.Connection)
        raw.rollback()
        pid, start = session_identity(raw)
        raw.rollback()
        open_context(app_engine_r, report_db, pid, start, secrets.token_hex(32))
        try:
            assert count_with_secret(raw, None) == 0
            assert raw.execute("SHOW eaios.report_ctx").fetchone() in (("",), None)
        finally:
            raw.rollback()
            close_context(app_engine_r, report_db, pid)


def test_context_cannot_be_opened_for_a_non_query_backend(
    report_db: DbHarness, app_engine_r: Engine
) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    digest = "0" * 64
    with (
        pytest.raises((InternalError, DBAPIError)),
        tenant_transaction(app_engine_r, tenant, user) as conn,
    ):
        conn.execute(
            text("SELECT report_context_open(pg_backend_pid(), now(), :d, :r, 30)"),
            {"d": digest, "r": str(uuid.uuid4())},
        )
    # Without a trusted tenant/user in the application transaction it also refuses.
    with pytest.raises((InternalError, DBAPIError)), app_engine_r.begin() as conn:
        conn.execute(
            text("SELECT report_context_open(1, now(), :d, :r, 30)"),
            {"d": digest, "r": str(uuid.uuid4())},
        )


# -- executor ---------------------------------------------------------------------------


def test_executor_returns_exact_frozen_q1_figures(
    report_db: DbHarness, executor: ReportExecutor
) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    result = executor.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=Q1_SQL)
    assert result.row_dicts() == [
        {
            "order_count": 291,
            "net_sales": "80934385.00",
            "tax_amount": "11330813.90",
            "gross_sales": "92265198.90",
        }
    ]
    assert [c["type"] for c in result.columns] == ["integer", "decimal", "decimal", "decimal"]
    assert result.parameters == {"row_limit": 51}
    assert Decimal(result.rows[0][1]) + Decimal(result.rows[0][2]) == Decimal(result.rows[0][3])


def test_isolation_tenant_context_sees_no_business_rows(
    report_db: DbHarness, executor: ReportExecutor
) -> None:
    tenant, user = ids(report_db, "isolation-test/finance-reader")
    result = executor.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=Q1_SQL)
    assert result.row_dicts() == [
        {"order_count": 0, "net_sales": None, "tax_amount": None, "gross_sales": None}
    ]


@pytest.fixture
def isolation_marker_rows(report_db: DbHarness) -> Iterator[dict[str, Any]]:
    """Two synthetic sales rows for the isolation tenant, removed after the test."""

    tenant, _ = ids(report_db, "isolation-test/finance-reader")
    customer, order = uuid.uuid4(), uuid.uuid4()
    owner_exec(
        report_db,
        "INSERT INTO sales_customers (tenant_id, id, name, region, country, since_date)"
        " VALUES (:t, :c, 'Isolation Marker Customer', 'GULF', 'AE', '2020-01-01')",
        {"t": tenant, "c": customer},
    )
    owner_exec(
        report_db,
        "INSERT INTO sales_orders (tenant_id, id, order_number, customer_id, order_date, region,"
        " status, subtotal, tax, total, currency) VALUES (:t, :o, 'ISO-MARKER-1', :c,"
        " '2026-02-02', 'GULF', 'FULFILLED', 777777.77, 108888.89, 886666.66, 'USD')",
        {"t": tenant, "o": order, "c": customer},
    )
    yield {"tenant": tenant, "order": order, "customer": customer}
    owner_exec(report_db, "DELETE FROM sales_orders WHERE id = :o", {"o": order})
    owner_exec(report_db, "DELETE FROM sales_customers WHERE id = :c", {"c": customer})


def test_cross_tenant_predicates_and_joins_return_nothing(
    report_db: DbHarness, executor: ReportExecutor, isolation_marker_rows: dict[str, Any]
) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    order, customer = isolation_marker_rows["order"], isolation_marker_rows["customer"]
    probes = [
        f"SELECT count(*) AS n FROM reporting.sales_orders WHERE id = '{order}'",
        "SELECT count(*) AS n FROM reporting.sales_orders WHERE subtotal = 777777.77",
        f"SELECT count(*) AS n FROM reporting.sales_orders o JOIN reporting.sales_customers c"
        f" ON c.id = o.customer_id WHERE c.id = '{customer}'",
        "SELECT count(*) AS n FROM reporting.sales_customers WHERE name LIKE 'Isolation%'",
    ]
    for sql in probes:
        result = executor.execute(
            tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=validate_sql(sql).deparsed
        )
        assert result.rows == [[0]], sql
    # The isolation tenant sees its own marker row and never NileTech rows.
    iso_tenant, iso_user = ids(report_db, "isolation-test/finance-reader")
    result = executor.execute(
        tenant_id=iso_tenant,
        user_id=iso_user,
        run_id=uuid.uuid4(),
        sql=validate_sql("SELECT order_number AS n FROM reporting.sales_orders").deparsed,
    )
    assert result.rows == [["ISO-MARKER-1"]]


def test_tenant_column_is_not_queryable(report_db: DbHarness, executor: ReportExecutor) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    with pytest.raises(QueryError) as excinfo:
        executor.execute(
            tenant_id=tenant,
            user_id=user,
            run_id=uuid.uuid4(),
            sql=validate_sql("SELECT tenant_id FROM reporting.sales_orders").deparsed,
        )
    assert excinfo.value.code == "database_error"


def test_excessive_results_are_rejected(report_db: DbHarness, executor: ReportExecutor) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    with pytest.raises(QueryError) as excinfo:
        executor.execute(
            tenant_id=tenant,
            user_id=user,
            run_id=uuid.uuid4(),
            sql=validate_sql("SELECT id FROM reporting.sales_orders").deparsed,
        )
    assert excinfo.value.code == "too_many_rows"


def test_statement_timeout(
    report_db: DbHarness, app_engine_r: Engine, report_engine: Engine
) -> None:
    fast = ReportExecutor(app_engine_r, report_engine, statement_timeout_ms=200, max_rows=5)
    tenant, user = ids(report_db, "niletech/finance-reader")
    heavy = validate_sql(
        "SELECT count(*) AS n FROM reporting.sales_order_lines a, reporting.sales_order_lines b,"
        " reporting.sales_order_lines c"
    ).deparsed
    with pytest.raises(QueryError) as excinfo:
        fast.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=heavy)
    assert excinfo.value.code == "statement_timeout"


def test_pool_reuse_does_not_leak_context(
    report_db: DbHarness, executor: ReportExecutor, report_engine: Engine
) -> None:
    """pool_size=1: every query reuses the same physical connection."""

    nile_t, nile_u = ids(report_db, "niletech/finance-reader")
    iso_t, iso_u = ids(report_db, "isolation-test/finance-reader")
    pids = set()
    for tenant, user, expected in (
        (nile_t, nile_u, 291),
        (iso_t, iso_u, 0),
        (nile_t, nile_u, 291),
        (iso_t, iso_u, 0),
    ):
        result = executor.execute(tenant_id=tenant, user_id=user, run_id=uuid.uuid4(), sql=Q1_SQL)
        assert result.rows[0][0] == expected
        with report_engine.connect() as conn:
            pids.add(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
            # Between uses, the pooled connection carries no context and sees nothing.
            assert (
                conn.execute(text("SELECT reporting_ctx.report_context_tenant()")).scalar() is None
            )
            assert conn.execute(text("SELECT count(*) FROM reporting.sales_orders")).scalar() == 0
            assert conn.execute(text("SELECT current_setting('app.tenant_id', true)")).scalar() in (
                None,
                "",
            )
    assert len(pids) == 1
    leftover = owner_exec(report_db, "SELECT count(*) FROM report_query_contexts").scalar_one()
    assert leftover == 0


def test_failed_query_still_closes_context(report_db: DbHarness, executor: ReportExecutor) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    with pytest.raises(QueryError):
        executor.execute(
            tenant_id=tenant,
            user_id=user,
            run_id=uuid.uuid4(),
            sql="SELECT 1/0 AS n",
        )
    assert owner_exec(report_db, "SELECT count(*) FROM report_query_contexts").scalar_one() == 0


# -- publication boundary and immutability ----------------------------------------------


def test_publication_lock_serializes_policy_changes(
    report_db: DbHarness, app_engine_r: Engine
) -> None:
    """While publication holds the tenant epoch lock, an authorization change waits."""

    tenant, user = ids(report_db, "niletech/finance-reader")
    group = report_db.foundation.tenants["niletech"].groups["finance-team"].id
    blocker = create_engine(report_db.owner_url, poolclass=pool.NullPool)
    try:
        with tenant_transaction(app_engine_r, tenant, user) as conn:
            conn.execute(text("SELECT lock_tenant_policy_epoch()"))
            with blocker.connect() as other:
                other.execute(text("SET lock_timeout = '300ms'"))
                with pytest.raises(DBAPIError) as excinfo:
                    other.execute(
                        text("DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u"),
                        {"g": group, "u": user},
                    )
                assert "lock" in str(excinfo.value).lower()
                other.rollback()
    finally:
        blocker.dispose()
    member = owner_exec(
        report_db,
        "SELECT count(*) FROM group_memberships WHERE group_id = :g AND user_id = :u",
        {"g": group, "u": user},
    ).scalar_one()
    assert member == 1


def test_evidence_rows_are_immutable(report_db: DbHarness, app_engine_r: Engine) -> None:
    tenant, user = ids(report_db, "niletech/finance-reader")
    resource = report_db.foundation.tenants["niletech"].resources["sales-reporting"].id
    run_id, evidence_id = uuid.uuid4(), uuid.uuid4()
    owner_exec(
        report_db,
        "INSERT INTO report_runs (id, tenant_id, owner_user_id, question, client_request_id,"
        " status, inference_mode, policy_epoch_at_start) VALUES (:id, :t, :u, 'q',"
        " :crid, 'failed', 'fake', 1)",
        {"id": run_id, "t": tenant, "u": user, "crid": uuid.uuid4().hex},
    )
    owner_exec(
        report_db,
        "INSERT INTO report_evidence (id, tenant_id, run_id, owner_user_id, resource_id, report_key,"
        " model_sql, executed_sql, parameters, columns, rows, row_count, semantics,"
        " dataset_fingerprint, policy_epoch, executed_at, result_sha256) VALUES (:id, :t, :r, :u,"
        " :res, 'sales.fulfilled', 's', 's', '{}', '[]', '[]', 0, '{}', 'fp', 1, now(), :h)",
        {"id": evidence_id, "t": tenant, "r": run_id, "u": user, "res": resource, "h": "0" * 64},
    )
    with pytest.raises(DBAPIError, match="immutable"):
        owner_exec(
            report_db,
            "UPDATE report_evidence SET rows = '[[1]]' WHERE id = :id",
            {"id": evidence_id},
        )
    with pytest.raises(ProgrammingError), tenant_transaction(app_engine_r, tenant, user) as conn:
        conn.execute(
            text("UPDATE report_evidence SET rows = '[[1]]' WHERE id = :id"), {"id": evidence_id}
        )
    with pytest.raises(ProgrammingError), tenant_transaction(app_engine_r, tenant, user) as conn:
        conn.execute(text("DELETE FROM report_evidence WHERE id = :id"), {"id": evidence_id})
    owner_exec(report_db, "DELETE FROM report_runs WHERE id = :id", {"id": run_id})
