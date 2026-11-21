"""Prompts and strict output parsing for the router, SQL and report agents.

Prompts carry the minimal reporting schema, the trusted intent and, for repairs,
rejection *codes*. They never carry other tenants' data, credentials or rows beyond
the verified figures the report agent must restate.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.reporting.contracts import COLUMN_LABELS, OUTPUT_COLUMNS, ReportProfile, SalesReportIntent

PROMPT_VERSION = "m2-prompts-v1"


class MalformedOutput(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


SCHEMA = """Schema "reporting" (PostgreSQL 17, read-only views):
- reporting.sales_orders(id uuid, order_number text, customer_id uuid, order_date date,
  region text, status text -- one of FULFILLED, PENDING, CANCELLED,
  subtotal numeric(14,2) -- tax-exclusive order amount,
  tax numeric(14,2), total numeric(14,2) -- subtotal + tax, currency text)
- reporting.sales_order_lines(id, order_id, product_id, quantity, unit_price, line_total)
- reporting.sales_invoices(id, order_id, invoice_number, issue_date, due_date, amount,
  currency, status -- PAID, OPEN, OVERDUE)
- reporting.sales_products(id, sku, name, tier, unit_price, currency, is_active)
- reporting.sales_customers(id, name, region, country, since_date)"""

REPAIR_HINTS = {
    "wrong_period": "The date range does not match the requested period exactly.",
    "unbounded_period": "Bound order_date on both sides.",
    "missing_date_filter": "Filter on reporting.sales_orders.order_date.",
    "wrong_date_column": "Use the order date (order_date), not invoice or other dates.",
    "wrong_status_filter": "Filter order status to exactly the included statuses.",
    "filter_outside_report_definition": (
        "Use no filters other than order_date, status and currency; no invoice filters."
    ),
    "gross_described_as_net": "net_sales must be sum(subtotal), which is tax-exclusive.",
    "net_described_as_gross": "gross_sales must be sum(total).",
    "wrong_tax_basis": "tax_amount must be sum(tax).",
    "measure_not_in_definition": (
        "Use order-level columns subtotal, tax and total from sales_orders."
    ),
    "wrong_order_count": "order_count must be count(*) over sales_orders.",
    "wrong_output_columns": "Return exactly the four named columns.",
    "join_fan_out": "Do not join order lines or invoices; that counts orders more than once.",
    "wrong_grain": "Return one total row: no GROUP BY, HAVING or DISTINCT.",
    "unverifiable_structure": (
        "Use one plain SELECT from reporting.sales_orders without CTEs or subqueries."
    ),
    "distinct_aggregate": "Do not use DISTINCT inside sums.",
    "currency_mismatch": "Report only the configured currency.",
    "value_changing_cast": "Do not cast or round amounts; return the sums as numeric.",
}


def _figure(value: Any, column: str, currency: str) -> str:
    if column == "order_count":
        return f"{int(value):,}"
    return f"{currency} {format_money(str(value))}"


def format_money(value: str) -> str:
    """Group digits of an exact 2-decimal amount. Never pads or rounds: a value with any
    other scale is refused, so a rounded amount cannot be displayed as if exact."""

    whole, dot, frac = value.partition(".")
    if not dot or len(frac) != 2 or not frac.isdigit():
        raise ValueError("money values must have exactly two decimals")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    return f"{sign}{int(whole):,}.{frac}"


def router_messages(question: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You route requests for an enterprise assistant. The only supported route is "
                '"sales_report": total fulfilled sales for one calendar quarter or month. '
                'Answer with JSON only: {"route": "sales_report"} or {"route": "unsupported"}.'
            ),
        },
        {"role": "user", "content": question},
    ]


def sql_messages(
    intent: SalesReportIntent, profile: ReportProfile, feedback: list[str] | None = None
) -> list[dict[str, str]]:
    statuses = ", ".join(intent.included_statuses)
    rules = (
        f"{SCHEMA}\n\n"
        "Write one PostgreSQL SELECT for this report definition:\n"
        f"- Period: {intent.period.label}: order_date from {intent.period.start.isoformat()}"
        f" through {intent.period.end.isoformat()}, both inclusive (date basis: order date).\n"
        f"- Included order statuses: {statuses}\n"
        f"- Currency: {intent.currency}. {profile.tax_treatment}.\n"
        "- Return exactly one row with these columns:\n"
        "  order_count = number of included orders,\n"
        "  net_sales = sum of subtotal, tax_amount = sum of tax, gross_sales = sum of total.\n"
        "- Read only reporting.sales_orders (alias o). No joins, CTEs, subqueries, GROUP BY,\n"
        "  LIMIT or functions other than count and sum. Use DATE 'YYYY-MM-DD' literals.\n"
        'Answer with JSON only: {"sql": "SELECT ..."}'
    )
    messages = [
        {"role": "system", "content": "You write safe, read-only PostgreSQL for reports."},
        {"role": "user", "content": rules},
    ]
    if feedback:
        hints = "\n".join(f"- {code}: {REPAIR_HINTS.get(code, code)}" for code in feedback)
        messages.append(
            {
                "role": "user",
                "content": f"The previous query was rejected:\n{hints}\nReturn a corrected query.",
            }
        )
    return messages


def report_messages(
    intent: SalesReportIntent,
    profile: ReportProfile,
    row: dict[str, Any],
    feedback: list[str] | None = None,
) -> list[dict[str, str]]:
    figures = "\n".join(
        f"- {column}: {_figure(row[column], column, intent.currency)}" for column in OUTPUT_COLUMNS
    )
    labels = ", ".join(f"{c} = {COLUMN_LABELS[c]}" for c in OUTPUT_COLUMNS)
    content = (
        f"Period: {intent.period.label}\n"
        f"Verified figures ({labels}):\n{figures}\n\n"
        f"Definition: only {', '.join(intent.included_statuses)} orders by order date; "
        f"{profile.tax_treatment}.\n"
        "Write 1 to 3 short sentences summarising these figures. Copy figures exactly as "
        "written above, with no other numbers, percentages, rounding or estimates. Cite the "
        "figure keys each sentence uses.\n"
        'Answer with JSON only: {"sentences": [{"text": "...", "cites": ["net_sales"]}]}'
    )
    messages = [
        {"role": "system", "content": "You summarise verified report figures faithfully."},
        {"role": "user", "content": content},
    ]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": "The previous summary failed verification:\n"
                + "\n".join(f"- {item}" for item in feedback)
                + "\nReturn a corrected summary.",
            }
        )
    return messages


_THINK = re.compile(r"<think>.*?</think>", re.S)
_FENCE = re.compile(r"```(?:json|sql)?\s*(.*?)```", re.S | re.I)


def _json_object(text: str) -> dict[str, Any]:
    cleaned = _THINK.sub("", text).strip()
    fenced = _FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise MalformedOutput("no_json_object")
    try:
        value = json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise MalformedOutput("invalid_json") from exc
    if not isinstance(value, dict):
        raise MalformedOutput("invalid_json")
    return value


def parse_route(text: str) -> str:
    route = _json_object(text).get("route")
    if route not in ("sales_report", "unsupported"):
        raise MalformedOutput("invalid_route")
    return str(route)


def parse_sql(text: str) -> str:
    sql = _json_object(text).get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise MalformedOutput("missing_sql")
    return sql


def parse_sentences(text: str) -> list[dict[str, Any]]:
    sentences = _json_object(text).get("sentences")
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= 3:
        raise MalformedOutput("invalid_sentences")
    out = []
    for item in sentences:
        if not isinstance(item, dict):
            raise MalformedOutput("invalid_sentences")
        sentence, cites = item.get("text"), item.get("cites")
        if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > 400:
            raise MalformedOutput("invalid_sentence_text")
        if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
            raise MalformedOutput("invalid_cites")
        out.append({"text": sentence.strip(), "cites": list(dict.fromkeys(cites))})
    return out


def premise(intent: SalesReportIntent, profile: ReportProfile, row: dict[str, Any]) -> str:
    """Deterministic statement of the verified evidence: the NLI premise."""

    c = intent.currency
    excluded = [s.lower() for s in profile.status_domain if s not in intent.included_statuses]
    count = _figure(row["order_count"], "order_count", c)
    net = _figure(row["net_sales"], "net_sales", c)
    tax = _figure(row["tax_amount"], "tax_amount", c)
    gross = _figure(row["gross_sales"], "gross_sales", c)
    period = intent.period
    return (
        f"In {period.label} (order dates {period.start.isoformat()} to "
        f"{period.end.isoformat()}), there were {count} fulfilled orders. "
        f"Net sales, which exclude tax, were {net}. Tax was {tax}. "
        f"Gross sales, which include tax, were {gross}. "
        f"Only {', '.join(intent.included_statuses)} orders are counted; "
        f"{' and '.join(excluded) or 'no other'} orders are excluded, and invoice payment "
        "status is not considered."
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
