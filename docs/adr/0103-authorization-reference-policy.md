# ADR 0103 — Reference authorization policy

Status: accepted for M0, pending planning-agent review. Source: [specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f) §8 "The five authorization layers" and "Keeping the filter equivalent"; roadmap "Permission semantics". It refines **ADR 0012, which the source still lists as Proposed**: the informal role-to-group mapping is replaced by this full reference policy.

Implementation: `backend/app/security/policy.py` (pure, no I/O). Fixtures: `backend/fixtures/foundation.toml`. Truth-table cases: `backend/fixtures/policy_cases.toml`.

## Inputs

- **Subject** (built by the server, ADR 0101): tenant, user, active flag, capability grants (capability plus optional department scope), group ids, clearance, country, department.
- **Action**: `resource.read` (content) or `resource.admin` (metadata administration). Any other action string is denied.
- **Resource metadata**: tenant, classification, owning department, `department_restricted`, `allowed_countries` (ISO codes, or `*` for any), active/deleted status, and grants (`allow` or `deny`; one user **or** one group; action `resource.read`).

M2 adds the content action **`dataset.query`** with its own capability and grant action, evaluated through the same five layers; see [ADR 0202](0202-report-authorization-and-row-scope.md).

## Defined capabilities and predicates

| Capability | Meaning |
| --- | --- |
| `resource.read` | May read resource content, subject to all five layers |
| `resource.admin` | May manage resource metadata and permissions; never implies read |

A role assignment may carry `scope_department`. `NULL` means tenant-wide; otherwise the capability applies only to resources whose owning department equals the scope. Fixture roles: `reader` → `resource.read`; `department_admin` → `resource.admin` (department-scoped); `access_admin` → `resource.admin` (tenant-wide).

| Predicate | Rule | Unknown handling |
| --- | --- | --- |
| Department | if `department_restricted`: user.department = resource.department | either side NULL → deny |
| Country | `*` in allowed_countries, or user.country ∈ allowed_countries; an empty list denies | user.country NULL with a restricted list → deny; list NULL → deny |
| Clearance | rank(user.clearance) ≥ rank(classification) | NULL or unrecognized value → deny |

Classification order: **public (0) < internal (1) < confidential (2) < restricted (3)**. Clearance uses the same scale.

## Evaluation order for `resource.read` (first failure wins)

1. Unknown action → `deny.unknown_action`
2. Subject inactive → `deny.inactive_subject`
3. **Tenant**: subject.tenant ≠ resource.tenant → `deny.tenant_mismatch` (public resources included; there is no anonymous access)
4. Resource deleted → `deny.resource_inactive`
5. Any matching **deny** grant (user, or any of the user's groups) → `deny.explicit_deny`; a malformed grant → `deny.unknown_input`
6. **Role**: no `resource.read` → `deny.missing_capability`; scope does not cover the department → `deny.capability_out_of_scope`
7. **Attribute**: department, then country → `deny.attribute_department` / `deny.attribute_country` / `deny.unknown_input`
8. **Grant**: no matching allow (direct user or group) → `deny.no_grant`
9. **Classification** → `deny.clearance` / `deny.unknown_input`
10. Otherwise **allow**

A direct user grant satisfies layer 8 only; it does not bypass tenant, role, attribute or clearance (fixture case: `isolation-test/board-member` → `payroll-report` is denied on country).

## `resource.admin`

Steps 1–4, then the `resource.admin` capability with a covering scope (`deny.missing_capability` / `deny.capability_out_of_scope`). Grants, attributes and clearance do not apply, and admin rights never satisfy `resource.read` (fixture: `finance-admin` reading `finance-report` → `deny.missing_capability`).

## Truth table (layer outcomes → decision)

| Tenant | Active | Deny grant | Capability (in scope) | Attributes | Allow grant (user or group) | Clearance ≥ class | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ✗ | any | any | any | any | any | any | deny (tenant) |
| ✓ | ✗ | any | any | any | any | any | deny (inactive) |
| ✓ | ✓ | ✓ | any | any | any | any | deny (explicit) |
| ✓ | ✓ | ✗ | ✗ | any | any | any | deny (capability) |
| ✓ | ✓ | ✗ | ✓ | ✗ / unknown | any | any | deny (attribute) |
| ✓ | ✓ | ✗ | ✓ | ✓ | ✗ | any | deny (no grant) |
| ✓ | ✓ | ✗ | ✓ | ✓ | ✓ | ✗ / unknown | deny (clearance) |
| ✓ | ✓ | ✗ | ✓ | ✓ | ✓ | ✓ | **allow** |

The concrete fixture cases (24) are in `policy_cases.toml`. Each is evaluated in memory and against PostgreSQL through the real application role. In the database run, another tenant's resource is invisible under RLS, so the reported reason is `deny.not_found`; callers must not distinguish it from absence (FR-04).

## Policy epoch

`tenants.policy_epoch` is a tenant-wide, monotonic counter. Row triggers raise it on insert or delete, and on relevant updates, of: `users` (status, clearance, country, department), `role_capabilities`, `user_roles`, `group_memberships`, `resources` (classification, department, department_restricted, allowed_countries, status) and `resource_grants`. Deleting a role or group cascades to rows that raise it. The epoch is exposed in the access context and recorded in authorization audit events. It is the invalidation key for later caches and eligible-ID snapshots (ADR 0104). It is not a per-user or per-document version; that refinement can come later without changing the evaluator.
