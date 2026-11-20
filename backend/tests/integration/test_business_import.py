"""NileTech sales import on real PostgreSQL: repeatability, reconciliation, RLS and the
frozen Q1 2026 report figures queried through the application role."""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.business_data.loader import (
    ImportMismatch,
    ImportRefused,
    ImportResult,
    database_fingerprint,
    import_sales_dataset,
)
from app.business_data.model import SalesDataset, dataset_fingerprint, normalize
from app.business_data.source import VerifiedSource, verify_vendored
from app.db.engine import contextless_transaction, tenant_transaction
from tests.integration.conftest import DbHarness, disposable_database, owner_exec

SPEC = tomllib.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/evaluation/sales-q1-2026.toml").read_text()
)
SALES_TABLES = (
    "sales_customers",
    "sales_products",
    "sales_orders",
    "sales_order_lines",
    "sales_invoices",
    "business_dataset_imports",
)
EXPECTED_COUNTS = {
    "customers": 120,
    "products": 25,
    "orders": 2400,
    "order_lines": 6067,
    "invoices": 2400,
}


@pytest.fixture(scope="module")
def source() -> VerifiedSource:
    return verify_vendored()


@pytest.fixture(scope="module")
def dataset(source: VerifiedSource) -> SalesDataset:
    return normalize(source)


@pytest.fixture(scope="module")
def imported(test_db: DbHarness, source: VerifiedSource, dataset: SalesDataset) -> ImportResult:
    return import_sales_dataset(test_db.owner_engine, source, dataset, environment="test")


def niletech(test_db: DbHarness) -> Any:
    return test_db.foundation.tenants["niletech"].id


def test_first_import_loads_every_row_and_matches_source(
    imported: ImportResult, dataset: SalesDataset
) -> None:
    assert imported.inserted == EXPECTED_COUNTS
    assert imported.row_counts == EXPECTED_COUNTS
    assert imported.database_fingerprint == dataset_fingerprint(dataset)
    assert imported.database_fingerprint == SPEC["normalized_fingerprint"]


def test_reimport_inserts_nothing_and_keeps_counts(
    imported: ImportResult, test_db: DbHarness, source: VerifiedSource, dataset: SalesDataset
) -> None:
    for _ in range(2):
        again = import_sales_dataset(test_db.owner_engine, source, dataset, environment="test")
        assert again.inserted == dict.fromkeys(EXPECTED_COUNTS, 0)
        assert again.row_counts == EXPECTED_COUNTS
        assert again.database_fingerprint == imported.database_fingerprint
    rows = owner_exec(test_db, "SELECT count(*) FROM business_dataset_imports").scalar_one()
    assert rows == 1


def test_import_manifest_records_provenance(imported: ImportResult, test_db: DbHarness) -> None:
    row = owner_exec(
        test_db,
        "SELECT dataset, generator_version, source_revision, seed, reference_date::text AS ref,"
        " source_content_fingerprint, normalized_fingerprint, row_counts"
        " FROM business_dataset_imports WHERE tenant_id = :t",
        {"t": niletech(test_db)},
    ).one()
    assert (row.dataset, row.generator_version, row.seed, row.ref) == (
        "niletech-0.1.6",
        "0.1.6",
        20260630,
        "2026-06-30",
    )
    assert row.source_revision == SPEC["source_revision"]
    assert row.source_content_fingerprint == SPEC["source_content_fingerprint"]
    assert row.normalized_fingerprint == SPEC["normalized_fingerprint"]
    assert row.row_counts == EXPECTED_COUNTS


def test_m0_identity_and_permission_fixtures_are_untouched(
    imported: ImportResult, test_db: DbHarness
) -> None:
    users = owner_exec(test_db, "SELECT count(*) FROM users").scalar_one()
    grants = owner_exec(test_db, "SELECT count(*) FROM resource_grants").scalar_one()
    expected_users = sum(len(t.users) for t in test_db.foundation.tenants.values())
    expected_grants = sum(
        len(r.grants) for t in test_db.foundation.tenants.values() for r in t.resources.values()
    )
    assert (users, grants) == (expected_users, expected_grants)


def test_two_clean_database_generations_are_identical(
    imported: ImportResult, source: VerifiedSource, dataset: SalesDataset
) -> None:
    with disposable_database(seed=True) as fresh:
        second = import_sales_dataset(fresh.owner_engine, source, dataset, environment="test")
        with fresh.owner_engine.connect() as conn:
            fp, counts = database_fingerprint(conn, second.tenant_id)
    assert second.inserted == EXPECTED_COUNTS
    assert (fp, counts) == (imported.database_fingerprint, imported.row_counts)


def test_import_refused_without_m0_tenant(source: VerifiedSource, dataset: SalesDataset) -> None:
    with disposable_database(seed=False) as empty, pytest.raises(ImportRefused, match="M0 seed"):
        import_sales_dataset(empty.owner_engine, source, dataset, environment="test")


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_import_refused_outside_development(
    test_db: DbHarness, source: VerifiedSource, dataset: SalesDataset, environment: str
) -> None:
    with pytest.raises(ImportRefused, match="development-only"):
        import_sales_dataset(test_db.owner_engine, source, dataset, environment=environment)


@pytest.fixture
def drifted_order(imported: ImportResult, test_db: DbHarness) -> Iterator[str]:
    order = SPEC["worked_subset"]["included_orders"][0]
    owner_exec(
        test_db,
        "UPDATE sales_orders SET subtotal = subtotal + 1 WHERE tenant_id = :t AND order_number = :o",
        {"t": niletech(test_db), "o": order},
    )
    yield order
    owner_exec(
        test_db,
        "UPDATE sales_orders SET subtotal = subtotal - 1 WHERE tenant_id = :t AND order_number = :o",
        {"t": niletech(test_db), "o": order},
    )


def test_reimport_detects_drifted_rows_and_changes_nothing(
    drifted_order: str, test_db: DbHarness, source: VerifiedSource, dataset: SalesDataset
) -> None:
    """A changed row is a mismatch with the source; the importer must not paper over it."""

    with pytest.raises(ImportMismatch, match="do not reproduce the source"):
        import_sales_dataset(test_db.owner_engine, source, dataset, environment="test")
    value = owner_exec(
        test_db,
        "SELECT subtotal FROM sales_orders WHERE tenant_id = :t AND order_number = :o",
        {"t": niletech(test_db), "o": drifted_order},
    ).scalar_one()
    assert value == Decimal("20641.00")


def test_different_dataset_already_imported_is_refused(
    imported: ImportResult, test_db: DbHarness, source: VerifiedSource, dataset: SalesDataset
) -> None:
    owner_exec(
        test_db,
        "UPDATE business_dataset_imports SET normalized_fingerprint = :fp WHERE tenant_id = :t",
        {"fp": "0" * 64, "t": niletech(test_db)},
    )
    try:
        with pytest.raises(ImportRefused, match="different fingerprint"):
            import_sales_dataset(test_db.owner_engine, source, dataset, environment="test")
    finally:
        owner_exec(
            test_db,
            "UPDATE business_dataset_imports SET normalized_fingerprint = :fp WHERE tenant_id = :t",
            {"fp": imported.normalized_fingerprint, "t": niletech(test_db)},
        )


# -- reconciliation in the database (importer checks, via the application role) --------

RECONCILIATION = {
    "order lines sum != subtotal": """
        SELECT count(*) FROM sales_orders o
         WHERE o.subtotal <> (SELECT coalesce(sum(l.line_total), 0) FROM sales_order_lines l
                               WHERE l.tenant_id = o.tenant_id AND l.order_id = o.id)""",
    "quantity * unit_price != line_total": """
        SELECT count(*) FROM sales_order_lines WHERE quantity * unit_price <> line_total""",
    "subtotal + tax != total": "SELECT count(*) FROM sales_orders WHERE subtotal + tax <> total",
    "invoice amount != order total": """
        SELECT count(*) FROM sales_invoices i
          JOIN sales_orders o ON o.tenant_id = i.tenant_id AND o.id = i.order_id
         WHERE i.amount <> o.total""",
    "orders without exactly one invoice": """
        SELECT count(*) FROM sales_orders o
         WHERE (SELECT count(*) FROM sales_invoices i
                 WHERE i.tenant_id = o.tenant_id AND i.order_id = o.id) <> 1""",
}


@pytest.mark.parametrize("check", list(RECONCILIATION))
def test_loaded_values_reconcile(
    imported: ImportResult, app_engine: Engine, test_db: DbHarness, check: str
) -> None:
    with tenant_transaction(app_engine, niletech(test_db)) as conn:
        assert conn.execute(text(RECONCILIATION[check])).scalar_one() == 0


def test_line_order_invoice_grand_totals_reconcile(
    imported: ImportResult, app_engine: Engine, test_db: DbHarness, dataset: SalesDataset
) -> None:
    with tenant_transaction(app_engine, niletech(test_db)) as conn:
        lines, subtotal, total, invoiced = conn.execute(
            text(
                "SELECT (SELECT sum(line_total) FROM sales_order_lines),"
                " (SELECT sum(subtotal) FROM sales_orders),"
                " (SELECT sum(total) FROM sales_orders),"
                " (SELECT sum(amount) FROM sales_invoices)"
            )
        ).one()
    assert lines == subtotal == sum((o.subtotal for o in dataset.orders), Decimal(0))
    assert total == invoiced == sum((o.total for o in dataset.orders), Decimal(0))


# -- the frozen report figures, queried as the asking identity's tenant -----------------

Q1_SQL = text(
    """
    SELECT count(*) AS n, coalesce(sum(subtotal), 0) AS net, coalesce(sum(tax), 0) AS tax,
           coalesce(sum(total), 0) AS gross
      FROM sales_orders
     WHERE order_date BETWEEN :start AND :end AND status = ANY(:statuses)
    """
)


def _q1(conn: Any, statuses: list[str] | None = None, orders: list[str] | None = None) -> Any:
    sql = Q1_SQL if orders is None else text(str(Q1_SQL) + " AND order_number = ANY(:orders)")
    params: dict[str, Any] = {
        "start": SPEC["quarter_start"],
        "end": SPEC["quarter_end"],
        "statuses": statuses or SPEC["included_statuses"],
    }
    if orders is not None:
        params["orders"] = orders
    return conn.execute(sql, params).one()


def test_full_quarter_sql_matches_frozen_case(
    imported: ImportResult, app_engine: Engine, test_db: DbHarness
) -> None:
    expected = next(c for c in SPEC["cases"] if c["id"] == "m1-sales-q1-2026-net-fulfilled")
    with tenant_transaction(app_engine, niletech(test_db)) as conn:
        row = _q1(conn)
        everything = _q1(conn, ["FULFILLED", "PENDING", "CANCELLED"])
    assert row.n == expected["expected_order_count"]
    assert (row.net, row.tax, row.gross) == (
        Decimal(expected["expected_net"]),
        Decimal(expected["expected_tax"]),
        Decimal(expected["expected_gross"]),
    )
    assert everything.net == Decimal(expected["distractor_all_status_net"])


def test_worked_subset_sql_matches_hand_checked_totals(
    imported: ImportResult, app_engine: Engine, test_db: DbHarness
) -> None:
    subset = SPEC["worked_subset"]
    every_order = [o["order"] for o in subset["orders"]]
    with tenant_transaction(app_engine, niletech(test_db)) as conn:
        row = _q1(conn, orders=every_order)
        included = set(
            conn.execute(
                text(
                    "SELECT order_number FROM sales_orders WHERE order_number = ANY(:o)"
                    " AND order_date BETWEEN :s AND :e AND status = 'FULFILLED'"
                ),
                {"o": every_order, "s": SPEC["quarter_start"], "e": SPEC["quarter_end"]},
            ).scalars()
        )
    assert included == set(subset["included_orders"])
    assert row.n == 5
    assert (row.net, row.tax, row.gross) == (
        Decimal(subset["expected_net"]),
        Decimal(subset["expected_tax"]),
        Decimal(subset["expected_gross"]),
    )


def test_cancelled_and_boundary_orders_never_count(
    imported: ImportResult, app_engine: Engine, test_db: DbHarness
) -> None:
    with tenant_transaction(app_engine, niletech(test_db)) as conn:
        statuses: dict[str, int] = {
            row[0]: row[1]
            for row in conn.execute(
                text(
                    "SELECT status, count(*) FROM sales_orders"
                    " WHERE order_date BETWEEN :s AND :e GROUP BY status"
                ),
                {"s": SPEC["quarter_start"], "e": SPEC["quarter_end"]},
            )
        }
        outside = conn.execute(
            text(
                "SELECT count(*) FROM sales_orders WHERE order_number = ANY(:o)"
                " AND order_date BETWEEN :s AND :e"
            ),
            {
                "o": ["SO-001667", "SO-001755", "SO-000268"],
                "s": SPEC["quarter_start"],
                "e": SPEC["quarter_end"],
            },
        ).scalar_one()
        cancelled_paid = conn.execute(
            text(
                "SELECT count(*) FROM sales_orders o JOIN sales_invoices i"
                " ON i.tenant_id = o.tenant_id AND i.order_id = o.id"
                " WHERE o.status = 'CANCELLED' AND i.status = 'PAID'"
            )
        ).scalar_one()
    assert statuses == {"FULFILLED": 291, "PENDING": 40, "CANCELLED": 28}
    assert outside == 0
    assert cancelled_paid == 196  # documented source anomaly, preserved as-is


# -- tenant isolation of business data ------------------------------------------------------


def test_business_tables_have_rls_and_are_read_only_for_app(
    imported: ImportResult, test_db: DbHarness, app_engine: Engine
) -> None:
    rows = owner_exec(
        test_db,
        "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(:n)",
        {"n": list(SALES_TABLES)},
    ).all()
    assert len(rows) == len(SALES_TABLES) and all(r.relrowsecurity for r in rows)
    for statement in (
        "UPDATE sales_orders SET status = 'FULFILLED'",
        "DELETE FROM sales_invoices",
        "INSERT INTO sales_products (tenant_id, id, sku, name, tier, unit_price, currency,"
        " is_active) VALUES (:t, gen_random_uuid(), 'X', 'X', 'X', 1, 'USD', true)",
    ):
        with (
            pytest.raises(ProgrammingError),
            tenant_transaction(app_engine, niletech(test_db)) as conn,
        ):
            conn.execute(text(statement), {"t": niletech(test_db)})


@pytest.mark.parametrize("table", SALES_TABLES)
def test_isolation_tenant_and_missing_context_see_no_business_rows(
    imported: ImportResult, test_db: DbHarness, app_engine: Engine, table: str
) -> None:
    other = test_db.foundation.tenants["isolation-test"].id
    with tenant_transaction(app_engine, other) as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
    with contextless_transaction(app_engine) as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608


def test_isolation_boundary_case_has_no_evidence(
    imported: ImportResult, test_db: DbHarness, app_engine: Engine
) -> None:
    """The frozen boundary case expects abstention: the same query finds nothing."""

    boundary = next(c for c in SPEC["cases"] if c["id"] == "m1-sales-q1-2026-isolation-boundary")
    tenant = test_db.foundation.tenants[boundary["tenant"]].id
    with tenant_transaction(app_engine, tenant) as conn:
        row = _q1(conn)
    assert row.n == 0 and row.net == 0
    assert all(Decimal(v) != row.net for v in boundary["forbidden_values"])
