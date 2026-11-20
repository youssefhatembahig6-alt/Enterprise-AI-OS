# NileTech business data baseline (0.1.6), sales subset

This directory holds the **verified sales subset** of the NileTech export used as the M1 business-data baseline. Nothing here is generated or edited by hand.

## Provenance

| Item | Value |
| --- | --- |
| Source archive | `niletech-0.1.6.zip` (owner-supplied, kept outside the repository in `reference-data/`) |
| Archive SHA-256 | `e220664ca64a21a78450fb1abcc649c45e1038a18064bc33aabadff866fb42d3` |
| Generator | version 0.1.6, commit `bf4a247a5c3db8bcedc37e0ed47dad6c635d9349` |
| Seed / reference date | `20260630` / `2026-06-30` |
| Embedded `manifest.json` SHA-256 | `885acf8aa2ccf5006a7e168cc8a52f4cbd81e3637052beb5cd815151c79ad99f` |
| Source content fingerprint | `36c7784f6754f44a17d08c96018f3bbea1512490079167a33df55a1f311122f0` — SHA-256 of `json.dumps(file_sha256, sort_keys=True, separators=(",", ":"))` (reproduced and checked by the loader) |
| Normalized sales fingerprint | `e5715a94f93ed42dd31b3f00337af4bb591495a0cfd96f7a5a8ca90555bef127` (see below) |
| Verified key counts | 1 company, 200 users, 70 document files, 2,400 orders, 2,400 invoices; also 6,067 order lines, 120 customers, 25 products |

## Files

- `source-manifest.json` — the archive's `manifest.json`, byte for byte.
- `tables/*.jsonl.gz` — `companies`, `customers`, `products`, `orders`, `order_lines` and `invoices`. Each is the original JSONL compressed with deterministic gzip (no file name, mtime 0, level 9). After decompression, each file's SHA-256 must equal the value in `source-manifest.json`.
- `vendor-manifest.json` — the subset's own manifest: provenance, source and gzip hashes, row counts, and the normalized fingerprint.

Users, HR records, documents, the payroll report and all other tables stay out of this public repository. Only the six sales tables are vendored.

Regenerate the subset from the archive with `python -m app.cli vendor-business-data --zip <path>`. The test `test_vendoring_twice_is_byte_identical_to_committed_fixture` checks that two runs, and the committed files, are byte-identical.

## Mapping to M0

| Source | Database | Notes |
| --- | --- | --- |
| `companies[0]` (`company_id 91fc82ba-…`, slug `niletech`) | M0 tenant with slug `niletech` | Must already exist (M0 seed). Recorded in `business_dataset_imports.source_company_id` |
| `customers.id`, `products.id`, `orders.id`, `order_lines.id`, `invoices.id` | `sales_*.id` (primary key `(tenant_id, id)`) | Source UUIDs are preserved unchanged |
| `order_number`, `invoice_number`, `sku` | same | Unique per tenant |
| `orders.sales_rep_id`, `customers.account_owner_id` | `sales_rep_source_id`, `account_owner_source_id` | Kept as source employee UUIDs, with no foreign key. Employees are not imported, and they are **not** M0 login accounts |
| `company_id`, `created_at`, `updated_at` | dropped | The generator sets every timestamp to the reference date |
| money strings | `numeric(14,2)` / Python `Decimal` | Exact; more than 2 decimal places is rejected |

M0 login accounts, roles, groups and resource grants are not changed by the import.

## Normalized fingerprint

Each table is sorted by `id` and every row is written as compact, key-sorted JSON, in the order customers, products, orders, order lines, invoices (each table preceded by `#name`). Money is written with exactly 2 places; dates and UUIDs as strings. The loader recomputes this fingerprint from the rows stored in PostgreSQL and requires it to match the source.

## Reporting definition (quarterly sales)

| Rule | Definition |
| --- | --- |
| Quarter | 2026-Q1 = `order_date` from **2026-01-01 to 2026-03-31, inclusive** |
| Date basis | Order date. The invoice issue date is not used |
| Included statuses | `FULFILLED` only. `PENDING` and `CANCELLED` are excluded |
| Tax | Net sales are tax-exclusive (`subtotal`). Tax is a flat 14% (`tax`); gross = `total` = subtotal + tax |
| Currency | USD, the only currency in the data; no conversion |
| Rounding | Source amounts are exact to 2 places; sums are exact and reported to 2 places, with no intermediate rounding |
| Invoice status | Payment status (`PAID`, `OPEN`, `OVERDUE`) does not affect sales |

The worked example and the frozen evaluation case are in [`docs/data/sales-report-q1-2026.md`](../../../../docs/data/sales-report-q1-2026.md) and [`fixtures/evaluation/sales-q1-2026.toml`](../../evaluation/sales-q1-2026.toml).

## Source anomalies (preserved, not corrected)

The baseline is **arithmetically consistent**: every line equals quantity × unit price, every order's lines sum to its subtotal, subtotal + tax = total, tax is exactly 14% at 2 places (half-up and half-even agree), every order has exactly one invoice, and each invoice amount equals its order total. The importer checks all of this.

The following **semantic** inconsistencies come from the generator. The importer reproduces them faithfully and reports them (`python -m app.cli business-data-report`); tests pin the counts.

| Code | Count | Meaning |
| --- | --- | --- |
| `CANCELLED_ORDER_INVOICE_PAID` | 196 | Cancelled order whose invoice is `PAID`, with no refund record |
| `CANCELLED_ORDER_INVOICE_OUTSTANDING` | 57 | Cancelled order whose invoice is still `OPEN` or `OVERDUE` |
| `PENDING_ORDER_INVOICE_PAID` | 208 | Pending (unfulfilled) order already paid |
| `INVOICE_OPEN_PAST_DUE_DATE` | 370 | `OPEN` invoice whose due date is before the reference date (expected `OVERDUE`) |
| `INVOICE_OVERDUE_BEFORE_DUE_DATE` | 11 | `OVERDUE` invoice not yet due at the reference date |
| `CUSTOMER_REGION_COUNTRY_MISMATCH` | 59 | Customer region inconsistent with country (for example EG or AE customers in `EUROPE`, EG in `GULF`, AE in `NORTH_AFRICA`) |

Order numbers are not chronological (for example `SO-000028` is dated 2026-04-01). This is noted but not treated as an anomaly.

**Effect on the sales report:** none. The report counts order status and order date only, so invoice-status anomalies do not change it. They matter for any future accounts-receivable or "collected revenue" question, which must state its own definition.

**Importer defects versus source anomalies.**
- An importer defect is a stored row that differs from the source. That is caught by the database fingerprint check, which rolls back the import, and by the reconciliation tests.
- A malformed value that cannot be stored faithfully stops normalization.
- A source anomaly is reported and loaded unchanged.
