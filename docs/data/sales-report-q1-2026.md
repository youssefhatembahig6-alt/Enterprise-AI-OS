# Worked sales-report example: NileTech, Q1 2026

Dataset `niletech-0.1.6` (generator commit `bf4a247a5c3db8bcedc37e0ed47dad6c635d9349`, seed 20260630, reference date 2026-06-30). Normalized fingerprint `e5715a94f93ed42dd31b3f00337af4bb591495a0cfd96f7a5a8ca90555bef127`. Frozen on 2026-11-20 in [`backend/fixtures/evaluation/sales-q1-2026.toml`](../../backend/fixtures/evaluation/sales-q1-2026.toml).

## Definition

- **Quarter:** order date 2026-01-01 to 2026-03-31, inclusive at both ends.
- **Date basis:** the order date, not the invoice date.
- **Included statuses:** `FULFILLED` only.
- **Net sales:** tax-exclusive, the sum of order `subtotal`. Tax is 14% (`tax`). Gross = `total`.
- **Currency and rounding:** USD; amounts are exact to 2 decimal places; no conversion and no intermediate rounding.
- Invoice payment status is ignored.

## Worked subset (10 orders) — this is NOT the full-quarter total

### Included (5)

| Order | Date | Status | Lines (qty × unit price = line total) | Subtotal (net) | Tax 14% | Total | Invoice |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| SO-001815 | 2026-01-01 | FULFILLED | NIL-0015: 5 × 4,128.00 = 20,640.00 | 20,640.00 | 2,889.60 | 23,529.60 | INV-001815, 2026-01-01, PAID |
| SO-001809 | 2026-01-01 | FULFILLED | NIL-0009: 9 × 47,089.00 = 423,801.00; NIL-0010: 12 × 10,558.00 = 126,696.00 | 550,497.00 | 77,069.58 | 627,566.58 | INV-001809, 2026-01-02, PAID |
| SO-001082 | 2026-01-14 | FULFILLED | NIL-0007: 1 × 5,840.00 = 5,840.00 | 5,840.00 | 817.60 | 6,657.60 | INV-001082, 2026-01-14, **OPEN** |
| SO-001890 | 2026-02-20 | FULFILLED | NIL-0015: 1 × 4,128.00 = 4,128.00 | 4,128.00 | 577.92 | 4,705.92 | INV-001890, 2026-02-22, PAID |
| SO-001990 | 2026-03-31 | FULFILLED | NIL-0015: 3 × 4,128.00 = 12,384.00 | 12,384.00 | 1,733.76 | 14,117.76 | INV-001990, **2026-04-04**, PAID |

### Arithmetic

```
SO-001809 lines:  423,801.00 + 126,696.00 = 550,497.00   (= subtotal)

Net  = 20,640.00 + 550,497.00 + 5,840.00 + 4,128.00 + 12,384.00
     = 571,137.00 + 5,840.00 + 4,128.00 + 12,384.00
     = 593,489.00

Tax  = 2,889.60 + 77,069.58 + 817.60 + 577.92 + 1,733.76 = 83,088.46
       check: 593,489.00 × 0.14 = 83,088.46

Gross = 593,489.00 + 83,088.46 = 676,577.46
       check: 23,529.60 + 627,566.58 + 6,657.60 + 4,705.92 + 14,117.76 = 676,577.46
```

**Subset result:** net (tax-exclusive) fulfilled sales = **USD 593,489.00**; tax USD 83,088.46; gross USD 676,577.46.

### Excluded (5)

| Order | Date | Status | Subtotal | Why excluded |
| --- | --- | --- | ---: | --- |
| SO-001667 | 2025-12-31 | FULFILLED | 288,392.00 | Ordered one day before the quarter |
| SO-001755 | 2025-12-31 | FULFILLED | 327,066.00 | Ordered in Q4 2025. Its invoice (INV-001755) was issued 2026-01-02, inside Q1, but the date basis is the order date |
| SO-001807 | 2026-01-02 | PENDING | 17,520.00 | Not fulfilled, even though INV-001807 is PAID (source anomaly) |
| SO-001818 | 2026-01-12 | CANCELLED | 41,373.00 | Cancelled, even though INV-001818 is PAID (source anomaly) |
| SO-000268 | 2026-04-01 | FULFILLED | 286,109.00 | Ordered one day after the quarter |

Two included orders show the same rule from the other side. SO-001990 counts because it was ordered on 2026-03-31, even though it was invoiced in Q2. SO-001082 counts although its invoice is unpaid.

## Full-quarter figures (separate from the subset)

These were computed by an independent Python pass over the verified source JSONL (`tests/unit/test_sales_reference.py`), not by hand and not by SQL. They are frozen in evaluation case `m1-sales-q1-2026-net-fulfilled`.

| Measure | Value |
| --- | ---: |
| Fulfilled orders in Q1 2026 | 291 |
| **Net fulfilled sales (tax-exclusive)** | **USD 80,934,385.00** |
| Tax (14%) | USD 11,330,813.90 |
| Gross fulfilled sales (tax-inclusive) | USD 92,265,198.90 |
| *Distractor:* net sales of all statuses (291 fulfilled + 40 pending + 28 cancelled) | USD 102,987,786.00 |
| *Excluded:* pending net / cancelled net | USD 12,605,562.00 / USD 9,447,839.00 |

80,934,385.00 + 12,605,562.00 + 9,447,839.00 = 102,987,786.00, so every Q1 order is counted exactly once.

## Frozen evaluation cases

| Case | Asking identity | Tenant | Permitted evidence | Expected | Side effects |
| --- | --- | --- | --- | --- | --- |
| `m1-sales-q1-2026-net-fulfilled` | `niletech/finance-reader` (M0 fixture login) | niletech | `sales_orders`, `sales_order_lines`, `sales_invoices` | Answer: USD 80,934,385.00 net, 291 orders | none (read-only) |
| `m1-sales-q1-2026-isolation-boundary` | `isolation-test/finance-reader` (M0 anonymous isolation fixture) | isolation-test | none | Abstention; must not reveal 80,934,385.00 or 593,489.00 | none |

The isolation-test tenant is part of M0's test fixtures only. It has no business data, and the same query run under its tenant context returns no rows (`test_isolation_boundary_case_has_no_evidence`). Which roles may query sales data is an M2 decision; this case records that the finance reader is the intended permitted identity.
