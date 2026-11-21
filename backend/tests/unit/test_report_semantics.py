"""Meaning checks on validated report SQL, routing and narrative claims (no database)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

import pytest

from app.reporting.claims import check_sentence
from app.reporting.contracts import Period, ReportProfile, SalesReportIntent
from app.reporting.router import extract_periods, route
from app.reporting.semantics import SemanticVerifier, check_probe
from app.reporting.sql_guard import validate_sql

PROFILE = ReportProfile(
    report_key="sales.fulfilled",
    resource_id=uuid.uuid4(),
    version=1,
    title="Fulfilled sales",
    currency="USD",
    date_basis="order_date",
    included_statuses=("FULFILLED",),
    status_domain=("FULFILLED", "PENDING", "CANCELLED"),
    net_measure="subtotal",
    tax_measure="tax",
    gross_measure="total",
    tax_treatment="net sales are tax-exclusive; gross = net + tax",
    periods=("quarter", "month"),
    fiscal_year_start_month=1,
)


def intent(
    start: date, end: date, label: str, kind: Literal["quarter", "month"] = "quarter"
) -> SalesReportIntent:
    return SalesReportIntent(
        report_key="sales.fulfilled",
        period=Period(kind=kind, start=start, end=end, label=label),
        date_basis="order_date",
        included_statuses=("FULFILLED",),
        currency="USD",
    )


Q1 = intent(date(2026, 1, 1), date(2026, 3, 31), "Q1 2026")
SELECT = (
    "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales, sum(o.tax) AS tax_amount,"
    " sum(o.total) AS gross_sales FROM reporting.sales_orders o "
)


def failures(sql: str, target: SalesReportIntent = Q1) -> list[str]:
    return SemanticVerifier(PROFILE, target).verify(validate_sql(sql)).failures


@pytest.mark.parametrize(
    "sql",
    [
        SELECT + "WHERE o.order_date >= DATE '2026-01-01' AND o.order_date < DATE '2026-04-01'"
        " AND o.status = 'FULFILLED'",
        SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31'"
        " AND o.status NOT IN ('PENDING', 'CANCELLED') AND o.currency = 'USD'",
        SELECT + "WHERE '2026-01-01' <= o.order_date AND o.order_date <= make_date(2026, 3, 31)"
        " AND o.status IN ('FULFILLED')",
        "SELECT COUNT(o.id) AS order_count, ROUND(SUM(o.total - o.tax), 2) AS net_sales,"
        " COALESCE(SUM(o.tax), 0) AS tax_amount, SUM(o.subtotal + o.tax)::numeric AS gross_sales"
        " FROM reporting.sales_orders AS o WHERE o.order_date BETWEEN DATE '2026-01-01'"
        " AND DATE '2026-03-31' AND o.status <> 'PENDING' AND o.status <> 'CANCELLED'",
    ],
)
def test_equivalent_correct_forms_pass(sql: str) -> None:
    assert failures(sql) == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # wrong quarter
        (
            SELECT
            + "WHERE o.order_date BETWEEN '2026-04-01' AND '2026-06-30' AND o.status = 'FULFILLED'",
            "wrong_period",
        ),
        # off-by-one: exclusive end written as inclusive
        (
            SELECT
            + "WHERE o.order_date >= '2026-01-01' AND o.order_date <= '2026-04-01' AND o.status = 'FULFILLED'",
            "wrong_period",
        ),
        # invoice date instead of order date
        (
            SELECT + "JOIN reporting.sales_invoices i ON i.order_id = o.id"
            " WHERE i.issue_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "wrong_date_column",
        ),
        # paid-only invoices
        (
            SELECT + "JOIN reporting.sales_invoices i ON i.order_id = o.id WHERE o.order_date"
            " BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED' AND i.status = 'PAID'",
            "filter_outside_report_definition",
        ),
        # all statuses
        (
            SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31'",
            "wrong_status_filter",
        ),
        (
            SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status IN"
            " ('FULFILLED', 'PENDING')",
            "wrong_status_filter",
        ),
        # gross described as net
        (
            "SELECT count(*) AS order_count, sum(o.total) AS net_sales, sum(o.tax) AS tax_amount,"
            " sum(o.total) AS gross_sales FROM reporting.sales_orders o WHERE o.order_date BETWEEN"
            " '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "gross_described_as_net",
        ),
        # net described as gross
        (
            "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales, sum(o.tax) AS tax_amount,"
            " sum(o.subtotal) AS gross_sales FROM reporting.sales_orders o WHERE o.order_date BETWEEN"
            " '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "net_described_as_gross",
        ),
        # worked subset presented as the quarter
        (
            SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status ="
            " 'FULFILLED' AND o.order_number IN ('SO-001815', 'SO-001809')",
            "filter_outside_report_definition",
        ),
        # grouping changes the grain
        (
            SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status ="
            " 'FULFILLED' GROUP BY o.region",
            "wrong_grain",
        ),
        # line-level measure
        (
            "SELECT count(*) AS order_count, sum(l.line_total) AS net_sales, sum(o.tax) AS tax_amount,"
            " sum(o.total) AS gross_sales FROM reporting.sales_orders o JOIN reporting.sales_order_lines"
            " l ON l.order_id = o.id WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND"
            " o.status = 'FULFILLED'",
            "measure_not_in_definition",
        ),
        # DISTINCT inside sum hides duplicates incorrectly
        (
            "SELECT count(*) AS order_count, sum(DISTINCT o.subtotal) AS net_sales, sum(o.tax) AS"
            " tax_amount, sum(o.total) AS gross_sales FROM reporting.sales_orders o WHERE o.order_date"
            " BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "distinct_aggregate",
        ),
        # OR is not verifiable
        (
            SELECT + "WHERE (o.order_date BETWEEN '2026-01-01' AND '2026-03-31' OR o.status ="
            " 'FULFILLED')",
            "filter_outside_report_definition",
        ),
        # missing date filter
        (SELECT + "WHERE o.status = 'FULFILLED'", "missing_date_filter"),
        # wrong currency
        (
            SELECT + "WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status ="
            " 'FULFILLED' AND o.currency = 'EUR'",
            "currency_mismatch",
        ),
        # missing column
        (
            "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales FROM reporting.sales_orders o"
            " WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "wrong_output_columns",
        ),
        # subquery
        (
            SELECT
            + "WHERE o.id IN (SELECT order_id FROM reporting.sales_invoices) AND o.order_date"
            " BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'",
            "unverifiable_structure",
        ),
    ],
)
def test_semantic_failures(sql: str, expected: str) -> None:
    assert expected in failures(sql)


def test_line_join_passes_ast_but_probe_detects_fan_out() -> None:
    sql = (
        SELECT + "JOIN reporting.sales_order_lines l ON l.order_id = o.id WHERE o.order_date"
        " BETWEEN '2026-01-01' AND '2026-03-31' AND o.status = 'FULFILLED'"
    )
    semantics = SemanticVerifier(PROFILE, Q1).verify(validate_sql(sql))
    assert semantics.ok and semantics.probe_sql is not None
    assert "count(DISTINCT o.id)" in semantics.probe_sql
    assert "sales_order_lines" in semantics.probe_sql
    check_probe(
        {"joined_rows": 731, "distinct_orders": 291, "currencies": 1, "currency": "USD"},
        PROFILE,
        semantics,
    )
    assert "join_fan_out" in semantics.failures


def test_verifier_generalizes_to_any_period() -> None:
    feb = intent(date(2026, 2, 1), date(2026, 2, 28), "February 2026", "month")
    good = (
        SELECT
        + "WHERE o.order_date BETWEEN '2026-02-01' AND '2026-02-28' AND o.status = 'FULFILLED'"
    )
    assert failures(good, feb) == []
    assert failures(good, Q1) == ["wrong_period"]


# -- routing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "kind", "label"),
    [
        (
            "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?",
            "sales_report",
            "Q1 2026",
        ),
        (
            "Net sales for Q1 2026 excluding pending/cancelled orders regardless of invoice status",
            "sales_report",
            "Q1 2026",
        ),
        ("How much did we sell in the fourth quarter of 2025?", "sales_report", "Q4 2025"),
        ("Fulfilled sales for February 2026, please.", "sales_report", "February 2026"),
        ("2026 Q2 revenue", "sales_report", "Q2 2026"),
        ("Give me the numbers for Q1 2026", "ask_model", "Q1 2026"),
    ],
)
def test_router_supported(question: str, kind: str, label: str) -> None:
    decision = route(question, PROFILE)
    assert decision.kind == kind
    assert decision.intent is not None and decision.intent.period.label == label


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("What were our sales?", "clarify"),
        ("Sales in 2026", "clarify"),
        ("Sales for Q1 2026 and Q2 2026", "clarify"),
        ("Sales for Q1 2026 including pending orders", "unsupported"),
        ("Total sales for Q1 2026 across all statuses", "unsupported"),
        ("Paid sales for Q1 2026", "unsupported"),
        ("Sales by invoice date for Q1 2026", "unsupported"),
        ("Q1 2026 sales by region", "unsupported"),
        ("Compare Q1 2026 sales versus Q4 2025", "unsupported"),
        ("Forecast sales for Q3 2026", "unsupported"),
        ("Q1 2026 sales in EUR", "unsupported"),
    ],
)
def test_router_abstains(question: str, kind: str) -> None:
    assert route(question, PROFILE).kind == kind


def test_period_extraction() -> None:
    (period,) = extract_periods("sales for Feb 2024")
    assert (period.start, period.end) == (date(2024, 2, 1), date(2024, 2, 29))


# -- narrative claims ------------------------------------------------------------------

ROW = {
    "order_count": 291,
    "net_sales": "80934385.00",
    "tax_amount": "11330813.90",
    "gross_sales": "92265198.90",
}


def claim(text: str, cites: list[str]) -> list[str]:
    return check_sentence(0, {"text": text, "cites": cites}, ROW, Q1).failures


def test_exact_cited_figures_pass() -> None:
    assert (
        claim(
            "In Q1 2026, net sales (tax-exclusive) were USD 80,934,385.00 from 291 fulfilled orders.",
            ["net_sales", "order_count"],
        )
        == []
    )
    assert (
        claim(
            "Between January 1 and March 31, 2026, gross sales including tax were USD 92,265,198.90.",
            ["gross_sales"],
        )
        == []
    )


@pytest.mark.parametrize(
    ("text", "cites", "code"),
    [
        ("Q1 2026 net sales were USD 593,489.00.", ["net_sales"], "figure_not_in_evidence"),
        ("Net sales were USD 102,987,786.00.", ["net_sales"], "figure_not_in_evidence"),
        ("Net sales were USD 92,265,198.90.", ["gross_sales"], "gross_described_as_net"),
        ("Net sales were USD 80,934,385.00.", ["order_count"], "figure_not_cited"),
        ("Net sales were about USD 80.9 million.", ["net_sales"], "rounded_or_scaled_figure"),
        ("Tax was 14% of net sales.", ["tax_amount"], "percentage_not_in_evidence"),
        ("Net sales were USD 80934385.", ["net_sales"], "figure_not_exact"),
        ("Net sales grew strongly.", [], "uncited_sentence"),
        ("Net sales were USD 80,934,385.00.", ["profit"], "unknown_citation"),
        ("Sales boomed this quarter.", ["net_sales"], "cited_figure_missing"),
        ("Net sales were USD 80,934,385.00.", ["net_sales", "tax_amount"], "cited_figure_missing"),
    ],
)
def test_bad_claims(text: str, cites: list[str], code: str) -> None:
    assert code in claim(text, cites)


# -- value-changing casts (review finding 1) ---------------------------------------------

Q1_WHERE = (
    " FROM reporting.sales_orders o WHERE o.order_date BETWEEN '2026-01-01' AND '2026-03-31'"
    " AND o.status = 'FULFILLED'"
)


def report_sql(
    net: str, tax: str = "sum(o.tax)", gross: str = "sum(o.total)", count: str = "count(*)"
) -> str:
    return (
        f"SELECT {count} AS order_count, {net} AS net_sales, {tax} AS tax_amount,"
        f" {gross} AS gross_sales" + Q1_WHERE
    )


@pytest.mark.parametrize(
    "net",
    [
        "sum(o.subtotal)::numeric(20,0)",
        "sum(o.subtotal)::numeric(20)",
        "sum(o.subtotal)::numeric(20,1)",
        "sum(o.subtotal)::numeric(20,4)",
        "sum(o.subtotal)::int8",
        "sum(o.subtotal)::integer",
        "CAST(sum(o.subtotal) AS bigint)",
        "coalesce(sum(o.subtotal)::numeric(20,0), 0)",
        "round(sum(o.subtotal)::int4, 2)",
        "sum(o.subtotal::integer)",
        "sum(o.subtotal::numeric(10,0))",
        "sum(o.subtotal::int8::numeric)",
        "round(sum(o.subtotal), 0)",
    ],
)
def test_value_changing_casts_are_rejected(net: str) -> None:
    found = failures(report_sql(net))
    assert found, net
    if "::" in net or "CAST" in net:
        assert "value_changing_cast" in found


def test_review_probe_lossy_casts_on_every_amount_are_rejected() -> None:
    sql = report_sql(
        "sum(o.subtotal)::numeric(20,0)",
        "sum(o.tax)::numeric(20,0)",
        "sum(o.total)::numeric(20,0)",
    )
    assert failures(sql).count("value_changing_cast") == 1  # recorded once
    assert "value_changing_cast" in failures(sql)


@pytest.mark.parametrize(
    "sql",
    [
        report_sql("sum(o.subtotal)::numeric"),
        report_sql("CAST(sum(o.subtotal) AS numeric(20, 2))"),
        report_sql("sum(o.subtotal::numeric)"),
        report_sql("coalesce(sum(o.subtotal), 0)::numeric(18,2)"),
        report_sql("sum(o.subtotal)", count="count(*)::bigint"),
    ],
)
def test_value_preserving_casts_are_accepted(sql: str) -> None:
    assert failures(sql) == []


def test_count_cast_to_narrower_type_is_rejected() -> None:
    assert "value_changing_cast" in failures(report_sql("sum(o.subtotal)", count="count(*)::int4"))


def test_money_formatting_never_pads_rounded_values() -> None:
    from app.reporting.prompts import format_money

    assert format_money("80934385.00") == "80,934,385.00"
    for bad in ("80934385", "80934385.0", "80934385.0000"):
        with pytest.raises(ValueError):
            format_money(bad)
