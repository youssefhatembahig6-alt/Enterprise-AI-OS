"""Independent reference for the M2 generalization periods (json + Decimal only).

Like test_sales_reference.py, this deliberately avoids SQL and the normalizer, so the
frozen expectations do not depend on the code under test.
"""

from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from app.business_data.source import verify_vendored

CASES = tomllib.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures/evaluation/sales-generalization-m2.toml"
    ).read_text()
)
RAW = verify_vendored()
ORDERS = [json.loads(line) for line in RAW.tables["orders"].splitlines() if line.strip()]


def test_cases_are_bound_to_the_verified_dataset() -> None:
    assert CASES["source_content_fingerprint"] == RAW.manifest["content_fingerprint"]
    assert CASES["reference_date"] == RAW.manifest["reference_date"]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["id"])
def test_period_reference(case: dict[str, object]) -> None:
    start, end = str(case["period_start"]), str(case["period_end"])
    in_period = [o for o in ORDERS if start <= o["order_date"] <= end]
    sales = [o for o in in_period if o["status"] == "FULFILLED"]

    def total(rows: list[dict[str, str]], field: str) -> Decimal:
        return sum((Decimal(o[field]) for o in rows), Decimal("0.00"))

    assert len(sales) == case["expected_order_count"]
    assert total(sales, "subtotal") == Decimal(str(case["expected_net"]))
    assert total(sales, "tax") == Decimal(str(case["expected_tax"]))
    assert total(sales, "total") == Decimal(str(case["expected_gross"]))
    assert total(in_period, "subtotal") == Decimal(str(case["distractor_all_status_net"]))
    assert end <= CASES["reference_date"]
