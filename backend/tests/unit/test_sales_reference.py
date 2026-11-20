"""Independent reference for the Q1 2026 sales figures and the hand-checked subset.

Deliberately does NOT use app.business_data.model or SQL: it reads the verified
raw JSONL with json + Decimal only, so the expected answer is independent of the
normalizer and of any query under test.
"""

from __future__ import annotations

import json
import tomllib
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.business_data.source import verify_vendored

CASE_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "evaluation" / "sales-q1-2026.toml"
SPEC = tomllib.loads(CASE_FILE.read_text())
RAW = verify_vendored()


def _rows(table: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in RAW.tables[table].splitlines() if line.strip()]


ORDERS = {o["order_number"]: o for o in _rows("orders")}
PRODUCTS = {p["id"]: p for p in _rows("products")}
INVOICES = {i["order_id"]: i for i in _rows("invoices")}
LINES: dict[str, list[dict[str, Any]]] = defaultdict(list)
for _line in _rows("order_lines"):
    LINES[_line["order_id"]].append(_line)


def in_quarter(order: dict[str, Any]) -> bool:
    return bool(SPEC["quarter_start"] <= order["order_date"] <= SPEC["quarter_end"])


def counts_as_sale(order: dict[str, Any]) -> bool:
    return in_quarter(order) and order["status"] in SPEC["included_statuses"]


def total(orders: list[dict[str, Any]], field: str) -> Decimal:
    return sum((Decimal(o[field]) for o in orders), Decimal("0.00"))


def case(case_id: str) -> dict[str, Any]:
    return next(c for c in SPEC["cases"] if c["id"] == case_id)


def test_case_is_bound_to_the_verified_dataset() -> None:
    assert SPEC["dataset"] == "niletech-0.1.6"
    assert SPEC["source_content_fingerprint"] == RAW.manifest["content_fingerprint"]
    assert SPEC["source_revision"] == RAW.manifest["source_revision"]
    assert {o["currency"] for o in ORDERS.values()} == {SPEC["currency"]}


def test_full_quarter_reference_matches_frozen_case() -> None:
    sales = [o for o in ORDERS.values() if counts_as_sale(o)]
    expected = case("m1-sales-q1-2026-net-fulfilled")
    assert len(sales) == expected["expected_order_count"]
    assert total(sales, "subtotal") == Decimal(expected["expected_net"])
    assert total(sales, "tax") == Decimal(expected["expected_tax"])
    assert total(sales, "total") == Decimal(expected["expected_gross"])
    every_status = [o for o in ORDERS.values() if in_quarter(o)]
    assert total(every_status, "subtotal") == Decimal(expected["distractor_all_status_net"])
    assert total(sales, "subtotal") != total(every_status, "subtotal")


def test_cancelled_and_pending_q1_orders_are_excluded() -> None:
    q1 = [o for o in ORDERS.values() if in_quarter(o)]
    by_status: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in q1:
        by_status[order["status"]].append(order)
    assert {k: len(v) for k, v in by_status.items()} == {
        "FULFILLED": 291,
        "PENDING": 40,
        "CANCELLED": 28,
    }
    assert not any(counts_as_sale(o) for o in by_status["CANCELLED"] + by_status["PENDING"])
    # Net of every status = fulfilled + pending + cancelled (no order is lost or double counted).
    assert total(q1, "subtotal") == sum(
        (total(v, "subtotal") for v in by_status.values()), Decimal(0)
    )


def test_quarter_boundaries_are_inclusive_on_both_ends() -> None:
    def day(date: str) -> list[dict[str, Any]]:
        return [o for o in ORDERS.values() if o["order_date"] == date]

    assert day("2025-12-31") and not any(in_quarter(o) for o in day("2025-12-31"))
    assert day("2026-01-01") and all(in_quarter(o) for o in day("2026-01-01"))
    assert day("2026-03-31") and all(in_quarter(o) for o in day("2026-03-31"))
    assert day("2026-04-01") and not any(in_quarter(o) for o in day("2026-04-01"))


def test_worked_subset_records_match_the_source_exactly() -> None:
    subset = SPEC["worked_subset"]
    for entry in subset["orders"]:
        order = ORDERS[entry["order"]]
        assert order["order_date"] == entry["order_date"]
        assert order["status"] == entry["status"]
        assert order["subtotal"] == entry["subtotal"]
        if "tax" in entry:
            assert (order["tax"], order["total"]) == (entry["tax"], entry["total"])
        if "lines" in entry:
            source_lines = sorted(
                (
                    PRODUCTS[li["product_id"]]["sku"],
                    li["quantity"],
                    li["unit_price"],
                    li["line_total"],
                )
                for li in LINES[order["id"]]
            )
            documented = sorted(
                (li["sku"], li["quantity"], li["unit_price"], li["line_total"])
                for li in entry["lines"]
            )
            assert source_lines == documented
        if "invoice" in entry:
            invoice = INVOICES[order["id"]]
            doc = entry["invoice"]
            assert (invoice["invoice_number"], invoice["issue_date"], invoice["status"]) == (
                doc["number"],
                doc["issue_date"],
                doc["status"],
            )
            assert invoice["amount"] == doc["amount"]
        decision = "include" if counts_as_sale(order) else "exclude"
        assert decision == entry["decision"], entry["order"]


def test_worked_subset_hand_arithmetic() -> None:
    """Re-derive the documented subset totals line by line from the documented values."""

    subset = SPEC["worked_subset"]
    included = [o for o in subset["orders"] if o["decision"] == "include"]
    assert [o["order"] for o in included] == subset["included_orders"]
    net = Decimal("0.00")
    tax = Decimal("0.00")
    for entry in included:
        line_sum = sum(
            (Decimal(li["unit_price"]) * li["quantity"] for li in entry["lines"]), Decimal("0.00")
        )
        assert line_sum == sum((Decimal(li["line_total"]) for li in entry["lines"]), Decimal(0))
        assert line_sum == Decimal(entry["subtotal"])
        assert (Decimal(entry["subtotal"]) * Decimal(SPEC["tax_rate"])).quantize(
            Decimal("0.01")
        ) == Decimal(entry["tax"])
        assert Decimal(entry["subtotal"]) + Decimal(entry["tax"]) == Decimal(entry["total"])
        net += Decimal(entry["subtotal"])
        tax += Decimal(entry["tax"])
    assert net == Decimal(subset["expected_net"]) == Decimal("593489.00")
    assert tax == Decimal(subset["expected_tax"]) == Decimal("83088.46")
    assert net + tax == Decimal(subset["expected_gross"]) == Decimal("676577.46")
    assert net != Decimal(case("m1-sales-q1-2026-net-fulfilled")["expected_net"])


def test_worked_subset_covers_every_rule() -> None:
    entries = {e["order"]: e for e in SPEC["worked_subset"]["orders"]}
    reasons = " ".join(e["reason"] for e in entries.values())
    statuses = {e["status"] for e in entries.values() if e["decision"] == "exclude"}
    assert {"CANCELLED", "PENDING", "FULFILLED"} <= statuses
    assert {e["order_date"] for e in entries.values()} >= {
        "2025-12-31",
        "2026-01-01",
        "2026-03-31",
        "2026-04-01",
    }
    assert "order-date basis" in reasons or "date basis is the order date" in reasons
    assert any(e.get("invoice", {}).get("status") == "OPEN" for e in entries.values())


def test_boundary_case_is_outside_the_business_dataset() -> None:
    boundary = case("m1-sales-q1-2026-isolation-boundary")
    assert boundary["tenant"] == "isolation-test" != RAW.manifest["tenant"]
    assert boundary["expected_kind"] == "abstention"
    assert boundary["permitted_evidence"] == []
    assert boundary["allowed_side_effects"] == []
