# ADR 0202 — Report authorization and row scope within a tenant

Status: accepted for M2, pending planning-agent review. Source: specification §8 five layers and "PostgreSQL: tenant and row restrictions enforced outside generated SQL"; M2 assignment "Authorize and preserve evidence" and the row-scope decision it requires. Implementation: `backend/app/security/policy.py`, `backend/app/reporting/access.py`, `backend/fixtures/foundation.toml`, `backend/fixtures/policy_cases.toml`.

## Decision

- A new action and capability, **`dataset.query`**, is evaluated by the existing reference policy through the same five layers as `resource.read`: tenant, role capability (with department scope), attributes (department restriction, countries), a grant for that same action (user or group; explicit deny wins), and clearance ≥ classification. Holding `resource.read` never implies `dataset.query`, and a `dataset.query` grant never implies `resource.read`. Admin rights imply neither.
- Each tenant has one protected **dataset resource** per supported report (`resources.kind = 'dataset'`), referenced by its `tenant_report_profiles` row. NileTech fixture: `sales-reporting` (confidential, finance, department-restricted, EG), allow for group `finance-team`, explicit deny for `denied-reader`. Only users with role `report_reader` hold the capability. Authenticated users do not get reporting by default.
- **Row scope within a tenant: whole-tenant dataset.** An allowed user may query every sales row of their own tenant. There is no finer row-level business permission (for example by region, customer or sales rep) in M2, and nothing in the UI or API implies one. Tenant isolation is enforced in PostgreSQL by RLS and the bound query context (ADR 0201); dataset authorization is enforced by the application before a query context can be opened, at evidence persistence, at publication and on every replay or evidence view.
- Authorization always uses the subject re-read from PostgreSQL (`load_subject`), not a request-time snapshot, so deactivation and role, group, grant or attribute changes apply to an in-flight run.

## Evidence

Truth table rows for `dataset.query` in `policy_cases.toml` (evaluated in memory and against PostgreSQL through the application role); workflow tests for the allowed finance reader, denied same-tenant users (sales reader, explicit-deny reader, board member), the isolation tenant, missing and deactivated users, and revocation at four points of a run (`tests/integration/test_report_workflow.py`).

## Consequences

Finer row permissions would need a second restrictive policy keyed to attributes carried in the query context, plus fixtures and tests; that is a separate decision, not implied here.
