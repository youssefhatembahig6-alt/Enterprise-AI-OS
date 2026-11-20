# Verified SQL report contract (M2)

Prepared 21 November 2026 for the first feature milestone: login → router → read-only SQL → report → verification → published result over SSE. Decisions: [ADR 0201](../adr/0201-restricted-report-sql.md) (query role and SQL surface), [ADR 0202](../adr/0202-report-authorization-and-row-scope.md) (authorization and row scope), [ADR 0203](../adr/0203-report-runs-events-publication.md) (runs, events, publication boundary, jobs), [ADR 0204](../adr/0204-report-verification.md) (verification). Model routes: [model-gateway.md](model-gateway.md). Schema: [openapi.json](../openapi.json).

## API

All routes need `Authorization: Bearer <session token>`. Unknown, foreign and no-longer-authorized runs and evidence all return the same `404 {"error": {"code": "not_found"}}`.

| Route | Purpose |
| --- | --- |
| `POST /api/v1/chat` `{question, client_request_id?}` | Create a run (idempotent on `client_request_id`, 8–64 of `[A-Za-z0-9_-]`) and stream its events (`text/event-stream`). Response header `X-Run-ID`. `503 reports_unavailable` when `REPORT_DATABASE_URL` is unset |
| `GET /api/v1/runs/{id}/events?after=N` | Replay events with id > N (or `Last-Event-ID`), then follow live ones. Re-authenticates and re-authorizes; see "Replay" |
| `GET /api/v1/runs/{id}` | Status, outcome, `last_event_id`, sanitized `diagnostics`, and the published `result` while the caller is still authorized |
| `POST /api/v1/runs/{id}/cancel` | `202`; sets `cancel_requested`; the worker stops before the next step and closes any model stream |
| `GET /api/v1/evidence/{id}` | Exact stored query result (owner only, published runs only, current authorization required), with an integrity check of its hash |

## Supported report and intent

One report kind, configured per tenant in `tenant_report_profiles` (`report_key = "sales.fulfilled"`). Code reads only the profile; there are no tenant-name branches.

| Profile field (NileTech fixture value) | Meaning |
| --- | --- |
| `resource_id` → `sales-reporting` dataset resource | What `dataset.query` is evaluated against |
| `currency` USD | Reported currency; any other currency filter or data fails |
| `date_basis` `order_date` | The only accepted date column |
| `included_statuses` [FULFILLED], `status_domain` [FULFILLED, PENDING, CANCELLED] | Status filter must select exactly the included set (equivalent `IN`, `=`, `<>`, `NOT IN` forms accepted) |
| `net_measure` subtotal, `tax_measure` tax, `gross_measure` total | Net is tax-exclusive; gross = net + tax |
| `periods` [quarter, month], `fiscal_year_start_month` 1 | One calendar quarter or month, inclusive at both ends |

The **intent** (`SalesReportIntent`) is derived by rules from the question and the profile, never from model output: report key, period (`kind`, `start`, `end` inclusive, `label`), date basis, statuses, currency, `tax_basis = net_is_tax_exclusive`, `grain = period_total`, output columns `order_count, net_sales, tax_amount, gross_sales`.

Routing outcomes: `sales_report`; `clarify` (no period, a year only, or more than one period); `unsupported` (other statuses, payment/invoice basis, breakdowns, comparisons, forecasts, other currencies, unsupported period kinds); `ask_model` (a period is named but no sales topic word: the model router may only answer `sales_report` or `unsupported`). Periods ending after the dataset's reference date abstain with `period_unavailable`.

## Supported SQL surface

Parsed with PostgreSQL 17's parser (pglast 7.22). Accepted: one `SELECT` built from allowlisted nodes, reading only `reporting.sales_orders`, `sales_order_lines`, `sales_invoices`, `sales_products`, `sales_customers` (security-invoker views without `tenant_id`), functions `sum count avg min max round abs date_trunc extract date_part make_date lower upper`, and casts to scalar types. Rejected by construction: DML and utility statements, multiple statements, writable or recursive CTEs, `SELECT INTO`, locking clauses, window functions, function scans, `TABLESAMPLE`, parameters, SQL value functions (`current_user`), system columns, schema-qualified functions, `regclass`-style casts, catalogs and `information_schema`. The executor runs the **deparsed** validated tree inside `SELECT * FROM (...) AS report_query LIMIT %(row_limit)s`.

For the report itself, the semantic verifier accepts only the shape documented in `backend/app/reporting/semantics.py`; see "Verification".

## Roles and capabilities

| Capability | Granted to (fixtures) | Grants |
| --- | --- | --- |
| `dataset.query` | role `report_reader`: NileTech finance-reader, denied-reader, inactive-reader; isolation-test finance-reader, denied-reader, inactive-reader | Run reports on a dataset resource, subject to all five policy layers with a `dataset.query` grant |
| `resource.read`, `resource.admin` | unchanged from M0 | Neither implies `dataset.query`; `dataset.query` grants do not imply `resource.read` |

The `sales-reporting` resource (kind `dataset`, confidential, finance, department-restricted, EG) has an allow grant for `finance-team` and an explicit deny for `denied-reader`. Allowed: the NileTech finance reader. Denied: sales reader (no capability), denied reader (explicit deny), board member (no capability), access admin (no capability), inactive reader. Row scope: see ADR 0202 — access is to the whole tenant sales dataset.

## Run states

`queued → running → published | abstained | failed | cancelled`. The run row stores owner, tenant, resource, question, `inference_mode` (`gateway` or `fake`), the policy epoch at start and at publication, the published `result` (only when `published`), and `stats` (timings, attempts, rejection codes, served model revisions; never draft text or rows). A durable `report_jobs` row is written in the same transaction (ADR 0203).

## Events

Six SSE events from the primary specification, section "Streaming", persisted per run with increasing ids (`id:` line) and `version` 1. Every payload carries `run_id` and `version`.

| Event | M2 use | Payload | When |
| --- | --- | --- | --- |
| `status` | Progress steps only | `step` (`accepted, authorization, routing, sql_generation, sql_validation, query_execution, query_verification, report_drafting, report_verification, publication`), `state` (`completed` or `retrying`), `label`, `inference`, optional `attempt`/`max_attempts`/`period`, `inference_label` in fake mode | Throughout |
| `token` | Pieces of the **verified** answer | `{message_id, part: "table", title, table, definition}` then `{message_id, part: "text", index, text, citations}` per verified sentence | Publication transaction only |
| `citation` | Evidence references | `{citation_id, evidence_id, row, column, label}` | Publication transaction only |
| `approval_required` | Reserved for wave 3 | — | Never in M2 |
| `error` | Failure | `{code, message}`: `model_unavailable`, `reports_unavailable`, `access_revoked`, `cancelled`, `internal_error` | Before `done` on failure |
| `done` | Terminal, exactly once (unique index) | `{message_id, outcome, verification, abstention, inference}`; `abstention = {code, message}` with codes `no_accessible_data`, `unsupported_request`, `clarification_needed`, `period_unavailable`, `verification_failed` | Last |

Heartbeats are SSE comments (`: heartbeat`) every 15 s and are not persisted. Before verification only `status` (and heartbeats) are sent. A refused stream ends with an unpersisted `error` (`access_revoked`, no `id`). Denied users and users whose tenant has no data receive the same `no_accessible_data` abstention.

**Replay.** `GET /runs/{id}/events` re-authenticates the session, checks ownership (else 404), and immediately before sending any `token`, `citation` or published `done` re-evaluates `dataset.query` from current PostgreSQL state. If access was revoked it sends only `error: access_revoked`. Live streams re-check the session every 5 s.

## Evidence

`report_evidence` stores, per accepted query: tenant, run, owner, resource, report key, the model's original SQL, the executed (deparsed) SQL, bound parameters, typed columns (`integer`, `decimal`, `date`, `text`, `boolean`), rows (decimals as exact strings), row count, semantics (intent, verified window/statuses/measures, join probe result), dataset fingerprint (`niletech-0.1.6:<normalized fingerprint>`), policy epoch, execution time and `result_sha256` over a canonical JSON of those fields. Rows cannot be updated (trigger, all roles) or deleted by the application role. Table cells and citations reference evidence by `{evidence_id, row, column}`. Rejected queries' results are never stored.

## Verification outcomes

`verification.label` on a published result:

| Label | Meaning |
| --- | --- |
| `figures_verified_narrative_dev_checked` | Query meaning and join multiplicity verified; every figure equals evidence exactly; each narrative sentence passed deterministic claim checks and scored ≥ 0.5 entailment from the **development NLI baseline** (uncalibrated; not the M7 trained checker) |
| `figures_verified_structured_only` | Same deterministic verification; no narrative shown because the checker or model was unavailable, or no draft passed within the repair bound (`narrative` = `withheld_checker_unavailable`, `withheld_model_unavailable` or `withheld_failed_verification`) |

`verification.inference_mode` is `gateway` or `fake`; fake runs also carry `inference_label` and are never model evidence.
