# ADR 0101 — Identity and session contract

Status: accepted for M0, pending planning-agent review. Source: [specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f) §8 "JWT authentication", §8 "Authentication to external services", FR-01, NFR-05.

## Decision

**Login.** `POST /api/v1/auth/login` with `{tenant, email, password}`. The tenant slug only selects where to look up the account; it is an identifier, not an authority, and is proven by the password. Passwords are hashed with Argon2id (argon2-cffi defaults). Unknown tenant, unknown user, wrong password, inactive user and inactive tenant all return the same `401 invalid_credentials` body, and one hash verification always runs to keep timing similar. Extra JSON fields are rejected (`422`), so a body cannot carry role, group, country or clearance values.

**Pre-authentication lookup.** The application database role cannot read users before a tenant context exists. Login calls `auth_lookup_login(slug, email)`, a `SECURITY DEFINER` function owned by the migration role, which returns at most one row (tenant id/status, user id/status, password hash) for an exact slug and normalized email match. It cannot enumerate tenants or users and leaves every tenant table protected (see ADR 0102).

**Access token (JWT).** HS256 only, signed with `JWT_SECRET` (≥32 bytes, backend environment only). Claims: `sub` (user id), `tenant_id`, `jti` (session id), `iat`, `exp`, `iss`, `aud`. All are required. TTL: `ACCESS_TOKEN_TTL_SECONDS`, default 900 s (15 min), allowed range 60–3600. Validation checks the header algorithm before decoding (`none`, other HMAC variants and asymmetric algorithms are rejected), then the signature, issuer, audience, expiry and UUID claim types. No leeway.

**Every protected request** sets the tenant context from the *verified* `tenant_id`, then loads from PostgreSQL the session (by `jti` and `sub`), user, tenant, role capabilities, groups, attributes and the current policy epoch. The token proves identity only; it never freezes permissions.

**Session validity.** A session is valid when its row exists for that user, `revoked_at` is null, `expires_at` is in the future (database clock), the user is `active` and the tenant is `active`. Otherwise: `401 unauthenticated`, with the private reason in the audit log only.

**Logout and revocation.** `POST /api/v1/auth/logout` sets `revoked_at` on that session only. Deactivating a user rejects every one of their sessions on the next request (the user check), without waiting for token expiry. Role, group, attribute and grant changes do not revoke sessions; they change the next authorization result because permissions are reloaded on every request (ADR 0103). Expired sessions are purged hourly by the scheduler (`purge_expired_sessions()`), one day after expiry.

**Browser storage (initial slice).** The web app keeps the access token in React state (memory) only — never `localStorage`, `sessionStorage` or cookies. ESLint forbids the storage globals. A reload requires signing in again. Refresh tokens need a separate contract (HttpOnly cookie, rotation, CSRF) and are out of M0. CORS lists the dev origins explicitly, with `allow_credentials=false`, because the token travels in the `Authorization` header.

**Session token vs connector credentials.** NFR-05's "tokens never reach the frontend" is read as: connector OAuth/refresh tokens, service credentials and signing keys stay server-side. The short-lived application session token is the one credential the browser holds, under the policy above.

**Error format.** Every error is `{"error": {"code", "message", "request_id"}}`. Codes used in M0: `invalid_credentials`, `unauthenticated`, `rate_limited`, `invalid_request`, `service_unavailable`, `not_found`, `internal_error`. Validation errors list field names, never submitted values.

**Rate limiting.** Failed logins count in Redis per SHA-256(tenant, email) — 5 per 15 min — and per SHA-256(client address) — 20 per 15 min. When either limit is reached, login returns `429 rate_limited` with `Retry-After`, even for a correct password. A successful login clears the identity counter. If Redis is unavailable, login fails closed with `503`. The client address is the direct peer; `X-Forwarded-For` is not trusted in M0.

## Consequences

- Revocation is effective on the next request, at the cost of one session/user/permission query per request.
- A stolen token is usable until logout, deactivation or expiry (at most 15 minutes by default).
- Changing `JWT_SECRET` invalidates every token; there is no key rotation (`kid`) yet.
