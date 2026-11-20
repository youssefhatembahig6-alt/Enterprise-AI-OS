"""Normalized NileTech sales records, canonical fingerprint and source checks.

Normalization is a pure function of the verified source bytes: no clock, no
randomness and no environment input. Money is ``Decimal`` with exactly two
places, dates are ``datetime.date``, and source UUIDs are kept as ``id``. The
source ``company_id`` must be the single company in ``companies`` and is
dropped (it maps to the M0 tenant at load time).

Two kinds of problems are kept apart:

* **Importer defects** — the normalized or loaded rows differ from the source.
  ``normalize`` raises :class:`NormalizationError` for malformed values, and the
  loader compares database fingerprints with the source fingerprint.
* **Source anomalies** — the source is internally inconsistent or semantically
  odd, but the importer reproduces it faithfully. ``source_anomalies`` reports
  them; the baseline is never rewritten.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.business_data.source import VerifiedSource

CENT = Decimal("0.01")
ORDER_STATUSES = frozenset({"FULFILLED", "PENDING", "CANCELLED"})
INVOICE_STATUSES = frozenset({"PAID", "OPEN", "OVERDUE"})


class NormalizationError(ValueError):
    """A source value could not be represented faithfully (importer-side failure)."""


def _money(value: Any, where: str) -> Decimal:
    if not isinstance(value, str):
        raise NormalizationError(
            f"{where}: money must be a decimal string, got {type(value).__name__}"
        )
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise NormalizationError(f"{where}: not a decimal: {value!r}") from exc
    if amount != amount.quantize(CENT):
        raise NormalizationError(f"{where}: more than two decimal places: {value!r}")
    return amount.quantize(CENT)


def _date(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise NormalizationError(f"{where}: not an ISO date: {value!r}") from exc


def _uuid(value: Any, where: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise NormalizationError(f"{where}: not a UUID: {value!r}") from exc


def _opt_uuid(value: Any, where: str) -> UUID | None:
    return None if value is None else _uuid(value, where)


@dataclass(frozen=True, slots=True)
class Customer:
    id: UUID
    name: str
    region: str
    country: str
    account_owner_source_id: UUID | None
    since_date: date


@dataclass(frozen=True, slots=True)
class Product:
    id: UUID
    sku: str
    name: str
    tier: str
    unit_price: Decimal
    currency: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class Order:
    id: UUID
    order_number: str
    customer_id: UUID
    sales_rep_source_id: UUID | None
    order_date: date
    region: str
    status: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class OrderLine:
    id: UUID
    order_id: UUID
    product_id: UUID
    quantity: int
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class Invoice:
    id: UUID
    order_id: UUID
    invoice_number: str
    issue_date: date
    due_date: date
    amount: Decimal
    currency: str
    status: str


@dataclass(frozen=True)
class SalesDataset:
    company_id: UUID
    company_slug: str
    reporting_currency: str
    customers: tuple[Customer, ...]
    products: tuple[Product, ...]
    orders: tuple[Order, ...]
    order_lines: tuple[OrderLine, ...]
    invoices: tuple[Invoice, ...]

    def row_counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in TABLE_ORDER}


TABLE_ORDER = ("customers", "products", "orders", "order_lines", "invoices")


def _company_rows(source: VerifiedSource, table: str, company_id: str) -> list[dict[str, Any]]:
    rows = source.records(table)
    foreign = [r.get("id") for r in rows if r.get("company_id") != company_id]
    if foreign:
        raise NormalizationError(f"{table}: rows for another company: {foreign[:3]}")
    return rows


def normalize(source: VerifiedSource) -> SalesDataset:
    companies = source.records("companies")
    if len(companies) != 1:
        raise NormalizationError(f"expected exactly one company, found {len(companies)}")
    company = companies[0]
    cid = str(company["id"])

    customers = tuple(
        sorted(
            (
                Customer(
                    id=_uuid(r["id"], "customers.id"),
                    name=str(r["name"]),
                    region=str(r["region"]),
                    country=str(r["country"]),
                    account_owner_source_id=_opt_uuid(r.get("account_owner_id"), "customers.owner"),
                    since_date=_date(r["since_date"], f"customers {r['id']}.since_date"),
                )
                for r in _company_rows(source, "customers", cid)
            ),
            key=lambda x: x.id,
        )
    )
    products = tuple(
        sorted(
            (
                Product(
                    id=_uuid(r["id"], "products.id"),
                    sku=str(r["sku"]),
                    name=str(r["name"]),
                    tier=str(r["tier"]),
                    unit_price=_money(r["unit_price"], f"products {r['id']}.unit_price"),
                    currency=str(r["currency"]),
                    is_active=bool(r["is_active"]),
                )
                for r in _company_rows(source, "products", cid)
            ),
            key=lambda x: x.id,
        )
    )
    orders = tuple(
        sorted(
            (
                Order(
                    id=_uuid(r["id"], "orders.id"),
                    order_number=str(r["order_number"]),
                    customer_id=_uuid(r["customer_id"], "orders.customer_id"),
                    sales_rep_source_id=_opt_uuid(r.get("sales_rep_id"), "orders.sales_rep_id"),
                    order_date=_date(r["order_date"], f"orders {r['id']}.order_date"),
                    region=str(r["region"]),
                    status=str(r["status"]),
                    subtotal=_money(r["subtotal"], f"orders {r['id']}.subtotal"),
                    tax=_money(r["tax"], f"orders {r['id']}.tax"),
                    total=_money(r["total"], f"orders {r['id']}.total"),
                    currency=str(r["currency"]),
                )
                for r in _company_rows(source, "orders", cid)
            ),
            key=lambda x: x.id,
        )
    )
    lines = []
    for r in _company_rows(source, "order_lines", cid):
        quantity = r["quantity"]
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise NormalizationError(f"order_lines {r['id']}.quantity is not an integer")
        lines.append(
            OrderLine(
                id=_uuid(r["id"], "order_lines.id"),
                order_id=_uuid(r["order_id"], "order_lines.order_id"),
                product_id=_uuid(r["product_id"], "order_lines.product_id"),
                quantity=quantity,
                unit_price=_money(r["unit_price"], f"order_lines {r['id']}.unit_price"),
                line_total=_money(r["line_total"], f"order_lines {r['id']}.line_total"),
            )
        )
    invoices = tuple(
        sorted(
            (
                Invoice(
                    id=_uuid(r["id"], "invoices.id"),
                    order_id=_uuid(r["order_id"], "invoices.order_id"),
                    invoice_number=str(r["invoice_number"]),
                    issue_date=_date(r["issue_date"], f"invoices {r['id']}.issue_date"),
                    due_date=_date(r["due_date"], f"invoices {r['id']}.due_date"),
                    amount=_money(r["amount"], f"invoices {r['id']}.amount"),
                    currency=str(r["currency"]),
                    status=str(r["status"]),
                )
                for r in _company_rows(source, "invoices", cid)
            ),
            key=lambda x: x.id,
        )
    )
    dataset = SalesDataset(
        company_id=_uuid(cid, "companies.id"),
        company_slug=str(company["slug"]),
        reporting_currency=str(company["reporting_currency"]),
        customers=customers,
        products=products,
        orders=orders,
        order_lines=tuple(sorted(lines, key=lambda x: x.id)),
        invoices=invoices,
    )
    _check_referential_integrity(dataset)
    return dataset


def _check_referential_integrity(ds: SalesDataset) -> None:
    """Dangling references cannot be loaded (FKs), so they stop the import."""

    customer_ids = {c.id for c in ds.customers}
    product_ids = {p.id for p in ds.products}
    order_ids = {o.id for o in ds.orders}
    problems = []
    problems += [
        f"order {o.order_number} -> unknown customer"
        for o in ds.orders
        if o.customer_id not in customer_ids
    ]
    problems += [
        f"line {line.id} -> unknown order"
        for line in ds.order_lines
        if line.order_id not in order_ids
    ]
    problems += [
        f"line {line.id} -> unknown product"
        for line in ds.order_lines
        if line.product_id not in product_ids
    ]
    problems += [
        f"invoice {i.invoice_number} -> unknown order"
        for i in ds.invoices
        if i.order_id not in order_ids
    ]
    for name in TABLE_ORDER:
        ids = [row.id for row in getattr(ds, name)]
        if len(ids) != len(set(ids)):
            problems.append(f"{name}: duplicate ids")
    if problems:
        raise NormalizationError("; ".join(problems[:10]))


# -- canonical fingerprint ---------------------------------------------------------


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.quantize(CENT), "f")
    if isinstance(value, date | UUID):
        return str(value)
    return value


def canonical_rows(rows: tuple[Any, ...] | list[Any]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append({f.name: _canonical(getattr(row, f.name)) for f in fields(row)})
    return sorted(out, key=lambda r: r["id"])


def fingerprint_tables(tables: dict[str, list[dict[str, Any]]]) -> str:
    """SHA-256 over canonical JSON lines of each table, in fixed table order."""

    digest = hashlib.sha256()
    for name in TABLE_ORDER:
        digest.update(f"#{name}\n".encode())
        for row in sorted(tables[name], key=lambda r: r["id"]):
            digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def dataset_fingerprint(ds: SalesDataset) -> str:
    return fingerprint_tables({name: canonical_rows(getattr(ds, name)) for name in TABLE_ORDER})


# -- source anomaly report -----------------------------------------------------------


@dataclass
class AnomalyReport:
    """Counts and up to five example identifiers per anomaly code."""

    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def add(self, code: str, example: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        bucket = self.examples.setdefault(code, [])
        if len(bucket) < 5:
            bucket.append(example)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Arithmetic consistency codes: expected to be zero in the 0.1.6 baseline.
ARITHMETIC_CODES = (
    "LINE_QUANTITY_PRICE_MISMATCH",
    "ORDER_LINES_SUBTOTAL_MISMATCH",
    "ORDER_SUBTOTAL_TAX_TOTAL_MISMATCH",
    "ORDER_TAX_RATE_NOT_14_PERCENT",
    "INVOICE_AMOUNT_ORDER_TOTAL_MISMATCH",
    "ORDER_WITHOUT_LINES",
    "ORDER_INVOICE_COUNT_NOT_ONE",
    "CURRENCY_NOT_REPORTING_CURRENCY",
    "UNKNOWN_STATUS",
)

TAX_RATE = Decimal("0.14")

# Countries consistent with each sales region. Egypt (EG) and the United Arab
# Emirates (AE) are both in EMEA; neither is in Europe.
REGION_COUNTRIES: dict[str, frozenset[str]] = {
    "EMEA": frozenset({"EG", "AE"}),
    "GULF": frozenset({"AE"}),
    "NORTH_AFRICA": frozenset({"EG"}),
    "EUROPE": frozenset(),
}


def source_anomalies(ds: SalesDataset, reference_date: date) -> AnomalyReport:
    report = AnomalyReport()
    lines_by_order: dict[UUID, list[OrderLine]] = defaultdict(list)
    for line in ds.order_lines:
        lines_by_order[line.order_id].append(line)
        if line.unit_price * line.quantity != line.line_total:
            report.add("LINE_QUANTITY_PRICE_MISMATCH", str(line.id))
    invoices_by_order: dict[UUID, list[Invoice]] = defaultdict(list)
    for invoice in ds.invoices:
        invoices_by_order[invoice.order_id].append(invoice)
        if invoice.status not in INVOICE_STATUSES:
            report.add("UNKNOWN_STATUS", invoice.invoice_number)
        if invoice.currency != ds.reporting_currency:
            report.add("CURRENCY_NOT_REPORTING_CURRENCY", invoice.invoice_number)
        if invoice.status == "OPEN" and invoice.due_date < reference_date:
            report.add("INVOICE_OPEN_PAST_DUE_DATE", invoice.invoice_number)
        if invoice.status == "OVERDUE" and invoice.due_date >= reference_date:
            report.add("INVOICE_OVERDUE_BEFORE_DUE_DATE", invoice.invoice_number)
    customers = {c.id: c for c in ds.customers}
    for order in ds.orders:
        ref = order.order_number
        if order.status not in ORDER_STATUSES:
            report.add("UNKNOWN_STATUS", ref)
        if order.currency != ds.reporting_currency:
            report.add("CURRENCY_NOT_REPORTING_CURRENCY", ref)
        lines = lines_by_order.get(order.id, [])
        if not lines:
            report.add("ORDER_WITHOUT_LINES", ref)
        elif sum((line.line_total for line in lines), Decimal(0)) != order.subtotal:
            report.add("ORDER_LINES_SUBTOTAL_MISMATCH", ref)
        if order.subtotal + order.tax != order.total:
            report.add("ORDER_SUBTOTAL_TAX_TOTAL_MISMATCH", ref)
        if (order.subtotal * TAX_RATE).quantize(CENT) != order.tax:
            report.add("ORDER_TAX_RATE_NOT_14_PERCENT", ref)
        invoices = invoices_by_order.get(order.id, [])
        if len(invoices) != 1:
            report.add("ORDER_INVOICE_COUNT_NOT_ONE", ref)
        for invoice in invoices:
            if invoice.amount != order.total:
                report.add("INVOICE_AMOUNT_ORDER_TOTAL_MISMATCH", ref)
            if order.status == "CANCELLED" and invoice.status == "PAID":
                report.add("CANCELLED_ORDER_INVOICE_PAID", ref)
            elif order.status == "CANCELLED":
                report.add("CANCELLED_ORDER_INVOICE_OUTSTANDING", ref)
            if order.status == "PENDING" and invoice.status == "PAID":
                report.add("PENDING_ORDER_INVOICE_PAID", ref)
            if invoice.issue_date < order.order_date:
                report.add("INVOICE_ISSUED_BEFORE_ORDER", ref)
        customer = customers[order.customer_id]
        if order.region != customer.region:
            report.add("ORDER_REGION_DIFFERS_FROM_CUSTOMER", ref)
    for customer in ds.customers:
        if customer.country not in REGION_COUNTRIES.get(customer.region, frozenset()):
            report.add("CUSTOMER_REGION_COUNTRY_MISMATCH", customer.name)
    return report


def status_cross_tab(ds: SalesDataset) -> Counter[tuple[str, str]]:
    invoice_status = {i.order_id: i.status for i in ds.invoices}
    return Counter((o.status, invoice_status.get(o.id, "-")) for o in ds.orders)
