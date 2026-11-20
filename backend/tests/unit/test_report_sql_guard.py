"""Parsed-SQL allowlist (no database). Database-side enforcement is tested separately in
tests/integration/test_report_sql_security.py, including bypasses of this validator."""

from __future__ import annotations

import pytest

from app.reporting.sql_guard import SqlRejected, validate_sql

GOOD = (
    "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales, sum(o.tax) AS tax_amount,"
    " sum(o.total) AS gross_sales FROM reporting.sales_orders o"
    " WHERE o.order_date >= DATE '2026-01-01' AND o.order_date <= DATE '2026-03-31'"
    " AND o.status = 'FULFILLED'"
)


def test_accepts_supported_select_and_runs_deparsed_tree() -> None:
    validated = validate_sql(GOOD + ";")
    assert validated.relations == {"sales_orders"}
    assert validated.deparsed.startswith("SELECT count(*) AS order_count")
    assert ";" not in validated.deparsed


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT 1; SELECT 2", "multiple_statements"),
        (GOOD + "; DELETE FROM reporting.sales_orders", "multiple_statements"),
        ("DELETE FROM reporting.sales_orders", "not_a_select"),
        ("UPDATE reporting.sales_orders SET subtotal = 0", "not_a_select"),
        ("INSERT INTO reporting.sales_orders (id) VALUES (gen_random_uuid())", "not_a_select"),
        (
            "WITH x AS (DELETE FROM reporting.sales_orders RETURNING *) SELECT * FROM x",
            "writable_cte",
        ),
        (
            "WITH x AS (UPDATE reporting.sales_orders SET tax = 0 RETURNING 1) SELECT 1",
            "writable_cte",
        ),
        ("SET app.tenant_id = '00000000-0000-0000-0000-000000000000'", "not_a_select"),
        ("SET ROLE eaios_owner", "not_a_select"),
        ("RESET ALL", "not_a_select"),
        ("SELECT set_config('app.tenant_id', 'x', false)", "function_not_allowed"),
        ("SELECT current_setting('app.tenant_id')", "function_not_allowed"),
        ("SELECT pg_sleep(10)", "function_not_allowed"),
        ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
        ("SELECT reporting_ctx.report_context_tenant()", "function_not_allowed"),
        ("SELECT public.report_context_open(1, NULL, 1)", "function_not_allowed"),
        ("SELECT dblink('x', 'y')", "function_not_allowed"),
        ("SELECT current_user", "disallowed_construct"),
        ("SELECT session_user", "disallowed_construct"),
        ("SELECT * FROM pg_catalog.pg_class", "relation_not_allowed"),
        ("SELECT * FROM pg_roles", "relation_not_allowed"),
        ("SELECT * FROM information_schema.tables", "relation_not_allowed"),
        ("SELECT * FROM public.users", "relation_not_allowed"),
        ("SELECT * FROM public.sales_orders", "relation_not_allowed"),
        ("SELECT * FROM users", "relation_not_allowed"),
        ("SELECT * FROM reporting.sales_orders FOR UPDATE", "locking_clause"),
        ("SELECT * INTO stolen FROM reporting.sales_orders", "select_into"),
        ("SELECT 'reporting.sales_orders'::regclass", "type_not_allowed"),
        ("SELECT ctid FROM reporting.sales_orders", "system_column"),
        ("SELECT * FROM generate_series(1, 10)", "disallowed_construct"),
        ("SELECT * FROM reporting.sales_orders TABLESAMPLE SYSTEM (10)", "disallowed_construct"),
        ("WITH RECURSIVE r AS (SELECT 1 UNION SELECT 1 FROM r) SELECT * FROM r", "recursive_cte"),
        ("SELECT sum(subtotal) OVER () FROM reporting.sales_orders", "window_function"),
        ("SELECT * FROM reporting.sales_orders o, LATERAL (SELECT 1) x", "lateral_subquery"),
        ("SELECT $1", "disallowed_construct"),
        ("COPY reporting.sales_orders TO STDOUT", "not_a_select"),
        ("DO $$ BEGIN PERFORM 1; END $$", "not_a_select"),
        ("EXPLAIN SELECT 1", "not_a_select"),
        ("WITH sales_orders AS (SELECT 1) SELECT * FROM sales_orders", "cte_shadows_relation"),
        ("SELEC 1", "parse_error"),
        ("", "empty_sql"),
        ("SELECT 1 /*" + "x" * 5000 + "*/", "sql_too_long"),
    ],
)
def test_rejects(sql: str, code: str) -> None:
    with pytest.raises(SqlRejected) as excinfo:
        validate_sql(sql)
    assert excinfo.value.code == code


def test_textual_select_prefix_is_not_enough() -> None:
    # Starts with SELECT, but the tree contains a function scan and a write CTE.
    for sql in (
        "SELECT * FROM (SELECT pg_terminate_backend(1)) x",
        "/* SELECT */ WITH d AS (DELETE FROM reporting.sales_orders RETURNING 1) SELECT 1",
    ):
        with pytest.raises(SqlRejected):
            validate_sql(sql)
