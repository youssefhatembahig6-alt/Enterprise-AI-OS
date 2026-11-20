"""Explicit, repeatable import of the verified NileTech sales data into PostgreSQL.

Runs with the privileged migration/owner role (like ``seed-dev``), only when
EAIOS_ENV is ``development`` or ``test``, and only into an existing M0 tenant
whose slug equals the source tenant. M0 login accounts and permission fixtures
are not touched.

Repeat safety: everything happens in one transaction that first locks and
reads the tenant's import record, then fingerprints the stored rows *before any
write*:

* no record and no rows   -> first import: insert every row, then the stored
                             rows must reproduce the source fingerprint
* record, rows match      -> repeat import: nothing is written
* record, rows differ     -> ImportMismatch; a deleted or changed row is
                             reported, never silently restored
* no record but rows      -> ImportMismatch (rows of unknown origin)
* record for another
  dataset / fingerprint   -> ImportRefused

Any failure rolls the transaction back, so nothing changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.business_data.model import (
    TABLE_ORDER,
    SalesDataset,
    dataset_fingerprint,
    fingerprint_tables,
)
from app.business_data.source import DATASET, VerifiedSource


class ImportRefused(RuntimeError):
    pass


class ImportMismatch(RuntimeError):
    """Rows in the database do not reproduce the source (importer defect or drift)."""


@dataclass(frozen=True)
class ImportResult:
    tenant_id: UUID
    inserted: dict[str, int]
    row_counts: dict[str, int]
    normalized_fingerprint: str
    database_fingerprint: str


_TABLE_NAMES = {
    "customers": "sales_customers",
    "products": "sales_products",
    "orders": "sales_orders",
    "order_lines": "sales_order_lines",
    "invoices": "sales_invoices",
}

_INSERTS: dict[str, str] = {
    "customers": (
        "INSERT INTO sales_customers (tenant_id, id, name, region, country,"
        " account_owner_source_id, since_date)"
        " VALUES (:tenant_id, :id, :name, :region, :country, :account_owner_source_id, :since_date)"
    ),
    "products": (
        "INSERT INTO sales_products"
        " (tenant_id, id, sku, name, tier, unit_price, currency, is_active)"
        " VALUES (:tenant_id, :id, :sku, :name, :tier, :unit_price, :currency, :is_active)"
    ),
    "orders": (
        "INSERT INTO sales_orders (tenant_id, id, order_number, customer_id, sales_rep_source_id,"
        " order_date, region, status, subtotal, tax, total, currency)"
        " VALUES (:tenant_id, :id, :order_number, :customer_id, :sales_rep_source_id, :order_date,"
        " :region, :status, :subtotal, :tax, :total, :currency)"
    ),
    "order_lines": (
        "INSERT INTO sales_order_lines (tenant_id, id, order_id, product_id, quantity, unit_price,"
        " line_total) VALUES (:tenant_id, :id, :order_id, :product_id, :quantity, :unit_price,"
        " :line_total)"
    ),
    "invoices": (
        "INSERT INTO sales_invoices (tenant_id, id, order_id, invoice_number, issue_date, due_date,"
        " amount, currency, status) VALUES (:tenant_id, :id, :order_id, :invoice_number,"
        " :issue_date, :due_date, :amount, :currency, :status)"
    ),
}

# Column lists for reading rows back, matching the normalized dataclass fields.
_SELECTS: dict[str, str] = {
    "customers": "SELECT id, name, region, country, account_owner_source_id, since_date"
    " FROM sales_customers WHERE tenant_id = :t",
    "products": "SELECT id, sku, name, tier, unit_price, currency, is_active"
    " FROM sales_products WHERE tenant_id = :t",
    "orders": "SELECT id, order_number, customer_id, sales_rep_source_id, order_date, region,"
    " status, subtotal, tax, total, currency FROM sales_orders WHERE tenant_id = :t",
    "order_lines": "SELECT id, order_id, product_id, quantity, unit_price, line_total"
    " FROM sales_order_lines WHERE tenant_id = :t",
    "invoices": "SELECT id, order_id, invoice_number, issue_date, due_date, amount, currency,"
    " status FROM sales_invoices WHERE tenant_id = :t",
}


def _canonical_db_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date | UUID):
        return str(value)
    return value


def database_fingerprint(conn: Connection, tenant_id: UUID) -> tuple[str, dict[str, int]]:
    """Fingerprint the tenant's sales rows exactly as stored (works for any role
    that can read them, including the RLS-restricted application role)."""

    tables: dict[str, list[dict[str, Any]]] = {}
    for name in TABLE_ORDER:
        result = conn.execute(text(_SELECTS[name]), {"t": tenant_id})
        keys = list(result.keys())
        tables[name] = [
            {k: _canonical_db_value(v) for k, v in zip(keys, row, strict=True)} for row in result
        ]
    return fingerprint_tables(tables), {name: len(rows) for name, rows in tables.items()}


def _rows(ds: SalesDataset, name: str, tenant_id: UUID) -> list[dict[str, Any]]:
    return [{"tenant_id": tenant_id, **asdict(row)} for row in getattr(ds, name)]


def import_sales_dataset(
    owner_engine: Engine,
    source: VerifiedSource,
    dataset: SalesDataset,
    *,
    environment: str,
) -> ImportResult:
    if environment not in ("development", "test"):
        raise ImportRefused(f"business-data import is development-only; EAIOS_ENV={environment!r}")
    normalized_fp = dataset_fingerprint(dataset)
    manifest = source.manifest
    with owner_engine.begin() as conn:
        tenant_id = conn.execute(
            # Row lock serializes concurrent imports into the same tenant.
            text("SELECT id FROM tenants WHERE slug = :slug FOR UPDATE"),
            {"slug": dataset.company_slug},
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ImportRefused(
                f"tenant {dataset.company_slug!r} does not exist; run the M0 seed first"
            )
        existing = conn.execute(
            text(
                "SELECT dataset, normalized_fingerprint FROM business_dataset_imports"
                " WHERE tenant_id = :t FOR UPDATE"
            ),
            {"t": tenant_id},
        ).one_or_none()
        if existing is not None and (existing.dataset, existing.normalized_fingerprint) != (
            DATASET,
            normalized_fp,
        ):
            raise ImportRefused(
                f"tenant already holds dataset {existing.dataset!r} with a different fingerprint"
            )

        expected_counts = dataset.row_counts()
        stored_fp, stored_counts = database_fingerprint(conn, tenant_id)
        if existing is not None:
            # Repeat import: the stored rows must already reproduce the source. Nothing
            # is inserted, so a deleted or changed row is reported, never silently restored.
            if stored_counts != expected_counts or stored_fp != normalized_fp:
                raise ImportMismatch(
                    "stored rows do not reproduce the previously imported source:"
                    f" counts={stored_counts} expected={expected_counts}"
                    f" fingerprint={stored_fp} expected={normalized_fp}"
                )
            return ImportResult(
                tenant_id=tenant_id,
                inserted=dict.fromkeys(TABLE_ORDER, 0),
                row_counts=stored_counts,
                normalized_fingerprint=normalized_fp,
                database_fingerprint=stored_fp,
            )
        if any(stored_counts.values()):
            raise ImportMismatch(
                f"tenant has sales rows but no import record: counts={stored_counts}"
            )

        # First import into an empty tenant.
        inserted: dict[str, int] = {}
        for name in TABLE_ORDER:
            table = _TABLE_NAMES[name]
            count_sql = text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t")  # noqa: S608
            before = conn.execute(count_sql, {"t": tenant_id}).scalar_one()
            rows = _rows(dataset, name, tenant_id)
            statement = text(_INSERTS[name])
            for start in range(0, len(rows), 1000):
                conn.execute(statement, rows[start : start + 1000])
            inserted[name] = conn.execute(count_sql, {"t": tenant_id}).scalar_one() - before

        db_fp, db_counts = database_fingerprint(conn, tenant_id)
        if db_counts != expected_counts or db_fp != normalized_fp:
            raise ImportMismatch(
                f"database rows do not reproduce the source: counts={db_counts}"
                f" expected={dataset.row_counts()} fingerprint={db_fp} expected={normalized_fp}"
            )

        conn.execute(
            text(
                """
                INSERT INTO business_dataset_imports
                    (tenant_id, dataset, generator_version, source_revision, seed, reference_date,
                     source_company_id, source_content_fingerprint, normalized_fingerprint,
                     row_counts)
                VALUES (:t, :dataset, :gen, :rev, :seed, :ref, :company, :content_fp, :norm_fp,
                        CAST(:counts AS jsonb))
                """
            ),
            {
                "t": tenant_id,
                "dataset": DATASET,
                "gen": manifest["generator_version"],
                "rev": manifest["source_revision"],
                "seed": manifest["seed"],
                "ref": date.fromisoformat(manifest["reference_date"]),
                "company": dataset.company_id,
                "content_fp": manifest["content_fingerprint"],
                "norm_fp": normalized_fp,
                "counts": json.dumps(dataset.row_counts(), sort_keys=True),
            },
        )
    return ImportResult(
        tenant_id=tenant_id,
        inserted=inserted,
        row_counts=db_counts,
        normalized_fingerprint=normalized_fp,
        database_fingerprint=db_fp,
    )
