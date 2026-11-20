# ADR 0204 — Report verification and repair bound

Status: accepted for M2, pending planning-agent review. Source: specification §6 "Hybrid verification", source ADR 0005/0018; roadmap "Verified answer behavior" and "Verification" contract; M2 assignment "Verify meaning and claims". Implementation: `backend/app/reporting/semantics.py`, `claims.py`, `workflow.py`, `router.py`.

## Decision

Verification never compares with a stored answer; it checks meaning and arithmetic for whatever period the trusted intent names.

1. **Intent from rules.** Period, statuses, date basis and currency come from the question and the tenant profile (`router.py`); the model cannot shift them.
2. **Query meaning (AST).** On the validated tree: the fact relation is `reporting.sales_orders`; the only date predicate is on `order_date` and its effective inclusive window equals the intent exactly (`>=`/`>`/`<=`/`<`/`=`/`BETWEEN`, `DATE`/cast/`make_date` literals); status predicates select exactly the included statuses (complements over the profile's status domain accepted); an optional currency filter must equal the profile's; no other filters (no invoice status, no order-number subsets, no `OR`); no `GROUP BY`/`HAVING`/`DISTINCT`, CTEs or subqueries; exactly the four named outputs, with `net_sales = sum(subtotal)` or `sum(total - tax)`, `tax_amount = sum(tax)` or `sum(total - subtotal)`, `gross_sales = sum(total)` or `sum(subtotal + tax)`, `order_count = count(*)`/`count(o.id)`; joins only on known keys.
3. **Join multiplicity (data).** A probe with the same `FROM`/`WHERE` returns joined rows, distinct orders and currencies through the same role and context. Joined rows ≠ distinct orders means double counting; more than one currency or a different currency fails.
4. **Result checks.** One row, integer count and decimal sums at cent precision; `order_count` equals the probe's distinct orders; net + tax = gross exactly (`Decimal`); zero orders abstains with `no_accessible_data`.
5. **Narrative claims.** Each sentence must cite evidence columns; every number must equal a cited cell exactly (money written with two decimals); every cited figure must appear; no percentages, scaled or rounded figures; net/gross wording must match the column. Then the development NLI checker scores each sentence against a deterministic premise built from the evidence; entailment < 0.5 fails.
6. **Repairs.** At most `REPORT_MAX_REPAIRS` (setting capped at 2) repairs after the first SQL draft and, separately, after the first narrative draft. Repair prompts carry rejection codes, not rows. Exhausted SQL repairs abstain (`verification_failed`). If the narrative cannot be verified (exhausted repairs, checker or model unavailable), only the deterministically verified table is published with label `figures_verified_structured_only` and the reason; rejected sentences are never stored in events, results or stats.

## Checker status

The checker is the public `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` at revision `6f5cf0a2b59cabb106aca4c287eed12e357e90eb`, a **development baseline, not the M7 trained checker**; the 0.5 threshold is uncalibrated. Results record model, revision, status and scores. Because the deterministic rules require every sentence to restate exact verified figures, the NLI baseline is a second layer, not the only guard.

## Evidence

`tests/unit/test_report_semantics.py` (equivalent correct forms, wrong quarter, off-by-one window, invoice date, paid-only, all statuses, gross as net, net as gross, worked subset, grouping, line measures, `DISTINCT` sums, `OR`, missing date, wrong currency, subqueries, fan-out probe, other periods; claim rules). `tests/integration/test_report_workflow.py` repairs each bad query and narrative, exhausts repairs, exercises checker rejection and failure, and publishes Q1 2026 plus three independently calculated periods (`fixtures/evaluation/sales-generalization-m2.toml`).
