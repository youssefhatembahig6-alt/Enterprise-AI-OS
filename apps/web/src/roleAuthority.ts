/**
 * Which role assignments the signed-in admin may change. Mirrors the backend rule in
 * backend/app/admin/service.py (`_may_assign`) and backend/app/security/policy.py
 * (`covers`). The UI uses it only to show controls; the backend still decides.
 *
 * - A scoped identity admin may only add or remove assignments scoped to one of
 *   their departments (never organization-wide ones).
 * - An assignment whose role carries an administrative capability also needs the
 *   admin to hold that capability for the assignment's scope (no escalation).
 * - Content capabilities (resource.read, dataset.query) need no such check.
 */

export interface CapabilityGrant {
  capability: string;
  scope_department: string | null;
}

export interface Assignment {
  role: string;
  scope_department: string | null;
}

export const ADMIN_CAPABILITIES = new Set([
  "resource.admin",
  "identity.admin",
  "audit.read",
  "tenant.config",
]);

/** null = organization-wide; a set = those departments (empty: not held). */
function scopeOf(grants: CapabilityGrant[], capability: string): Set<string> | null {
  const held = grants.filter((g) => g.capability === capability);
  if (held.some((g) => g.scope_department === null)) return null;
  return new Set(held.map((g) => g.scope_department as string));
}

export function covers(grants: CapabilityGrant[], capability: string, department: string | null): boolean {
  const scope = scopeOf(grants, capability);
  if (scope === null) return true;
  return department !== null && scope.has(department);
}

export function canAssign(
  grants: CapabilityGrant[],
  roleCapabilities: Map<string, string[]>,
  assignment: Assignment,
): boolean {
  const caps = roleCapabilities.get(assignment.role);
  if (!caps) return false; // unknown role: never treated as editable
  const identityScope = scopeOf(grants, "identity.admin");
  if (identityScope !== null) {
    if (assignment.scope_department === null || !identityScope.has(assignment.scope_department)) {
      return false;
    }
  }
  return caps
    .filter((c) => ADMIN_CAPABILITIES.has(c))
    .every((c) => covers(grants, c, assignment.scope_department));
}

export function formatAssignment(a: Assignment): string {
  return a.scope_department ? `${a.role}@${a.scope_department}` : a.role;
}

export function parseAssignments(text: string): Assignment[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const [role, scope] = part.split("@");
      return { role: (role ?? "").trim(), scope_department: scope ? scope.trim() : null };
    });
}

export function sameAssignment(a: Assignment, b: Assignment): boolean {
  return a.role === b.role && a.scope_department === b.scope_department;
}
