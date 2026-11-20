"""Normalization (importer side) versus source anomalies (data side)."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.business_data.model import (
    ARITHMETIC_CODES,
    NormalizationError,
    SalesDataset,
    normalize,
    source_anomalies,
    status_cross_tab,
)
from app.business_data.source import VerifiedSource, verify_vendored

SOURCE = verify_vendored()
DATASET = normalize(SOURCE)
REFERENCE_DATE = date(2026, 6, 30)

# Semantic anomalies present in the untouched 0.1.6 baseline. They are reported,
# documented in fixtures/business/niletech-0.1.6/README.md and never corrected.
BASELINE_ANOMALIES = {
    "CANCELLED_ORDER_INVOICE_PAID": 196,
    "CANCELLED_ORDER_INVOICE_OUTSTANDING": 57,
    "PENDING_ORDER_INVOICE_PAID": 208,
    "INVOICE_OPEN_PAST_DUE_DATE": 370,
    "INVOICE_OVERDUE_BEFORE_DUE_DATE": 11,
    "CUSTOMER_REGION_COUNTRY_MISMATCH": 59,
}


def test_row_counts() -> None:
    assert DATASET.row_counts() == {
        "customers": 120,
        "products": 25,
        "orders": 2400,
        "order_lines": 6067,
        "invoices": 2400,
    }
    assert DATASET.company_slug == "niletech"
    assert DATASET.reporting_currency == "USD"


def test_money_is_exact_decimal() -> None:
    assert all(isinstance(o.subtotal, Decimal) for o in DATASET.orders)
    assert all(o.total.as_tuple().exponent == -2 for o in DATASET.orders)


def test_baseline_is_arithmetically_consistent_and_semantic_anomalies_are_pinned() -> None:
    report = source_anomalies(DATASET, REFERENCE_DATE)
    assert not any(report.counts.get(code) for code in ARITHMETIC_CODES), report.counts
    assert report.counts == BASELINE_ANOMALIES


def test_status_cross_tab_matches_documentation() -> None:
    assert status_cross_tab(DATASET) == {
        ("FULFILLED", "PAID"): 1413,
        ("FULFILLED", "OPEN"): 313,
        ("FULFILLED", "OVERDUE"): 151,
        ("PENDING", "PAID"): 208,
        ("PENDING", "OPEN"): 45,
        ("PENDING", "OVERDUE"): 17,
        ("CANCELLED", "PAID"): 196,
        ("CANCELLED", "OPEN"): 36,
        ("CANCELLED", "OVERDUE"): 21,
    }


def _with_changed_line(ds: SalesDataset, delta: str) -> SalesDataset:
    first = ds.order_lines[0]
    changed = dataclasses.replace(first, line_total=first.line_total + Decimal(delta))
    return dataclasses.replace(ds, order_lines=(changed, *ds.order_lines[1:]))


def test_injected_source_inconsistency_is_reported_not_corrected() -> None:
    corrupted = _with_changed_line(DATASET, "1.00")
    report = source_anomalies(corrupted, REFERENCE_DATE)
    assert report.counts["LINE_QUANTITY_PRICE_MISMATCH"] == 1
    assert report.counts["ORDER_LINES_SUBTOTAL_MISMATCH"] == 1
    # The record itself is untouched by the checker.
    assert corrupted.order_lines[0].line_total == DATASET.order_lines[0].line_total + 1


def test_injected_invoice_and_tax_inconsistencies_are_reported() -> None:
    order = DATASET.orders[0]
    bad_order = dataclasses.replace(order, tax=order.tax + Decimal("0.01"))
    invoice = next(i for i in DATASET.invoices if i.order_id == order.id)
    bad_invoice = dataclasses.replace(invoice, amount=invoice.amount - Decimal("5.00"))
    ds = dataclasses.replace(
        DATASET,
        orders=(bad_order, *DATASET.orders[1:]),
        invoices=tuple(bad_invoice if i is invoice else i for i in DATASET.invoices),
    )
    counts = source_anomalies(ds, REFERENCE_DATE).counts
    assert counts["ORDER_SUBTOTAL_TAX_TOTAL_MISMATCH"] == 1
    assert counts["ORDER_TAX_RATE_NOT_14_PERCENT"] == 1
    assert counts["INVOICE_AMOUNT_ORDER_TOTAL_MISMATCH"] == 1


def _source_with(table: str, mutate: Any) -> VerifiedSource:
    rows = SOURCE.records(table)
    mutate(rows[0])
    import json

    tables = dict(SOURCE.tables)
    tables[table] = b"".join(json.dumps(r).encode() + b"\n" for r in rows)
    return dataclasses.replace(SOURCE, tables=tables)


@pytest.mark.parametrize(
    ("table", "mutate", "message"),
    [
        ("orders", lambda r: r.update(subtotal="10.005"), "more than two decimal places"),
        ("orders", lambda r: r.update(total=10.5), "money must be a decimal string"),
        ("order_lines", lambda r: r.update(quantity="2"), "not an integer"),
        ("invoices", lambda r: r.update(issue_date="2026-02-30"), "not an ISO date"),
        (
            "orders",
            lambda r: r.update(company_id="00000000-0000-0000-0000-000000000000"),
            "another company",
        ),
        (
            "order_lines",
            lambda r: r.update(order_id="00000000-0000-0000-0000-000000000001"),
            "unknown order",
        ),
    ],
)
def test_unrepresentable_values_stop_the_importer(table: str, mutate: Any, message: str) -> None:
    """These would make the import lossy or impossible, so they fail loudly."""

    with pytest.raises(NormalizationError, match=message):
        normalize(_source_with(table, mutate))
