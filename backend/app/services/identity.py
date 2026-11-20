"""Login, logout and per-request access-context construction."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine

from app.config import Settings
from app.db.engine import contextless_transaction, tenant_transaction
from app.repositories import identity as repo
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext
from app.security.passwords import PasswordVerifier, VerifierBusyError
from app.security.policy import CapabilityGrant, Subject
from app.security.ratelimit import LoginRateLimiter, RateLimiterUnavailable, Reservation
from app.security.tokens import InvalidTokenError, decode_access_token, issue_access_token

logger = logging.getLogger("eaios.identity")


class LoginFailed(Exception):
    pass


class LoginRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class LoginBusy(Exception):
    """All password-verification slots stayed busy for the queue timeout."""


class AuthenticationFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    expires_at: datetime


class IdentityService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        limiter: LoginRateLimiter,
        verifier: PasswordVerifier,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._limiter = limiter
        self._verifier = verifier

    # -- login -----------------------------------------------------------------

    def login(
        self, *, tenant_slug: str, email: str, password: str, client: str, request_id: str | None
    ) -> LoginResult:
        """Admit, verify and issue a session.

        Order: (1) atomically reserve admission in Redis, (2) take a bounded
        verification slot, (3) run Argon2, (4) settle the reservation. A verified
        credential failure keeps its reservation; success clears it; any other
        exit (lookup error, busy verifier, database error) refunds it.
        """

        email = email.strip().lower()
        tenant_slug = tenant_slug.strip().lower()

        admission = self._limiter.reserve(tenant_slug, email, client)
        if not isinstance(admission, Reservation):
            self._audit_login_denied(request_id, None, None, "rate_limited", tenant_slug)
            raise LoginRateLimited(admission)

        outcome = "error"
        try:
            result = self._login_admitted(tenant_slug, email, password, request_id)
            outcome = "success"
            return result
        except LoginFailed:
            outcome = "failure"
            raise
        finally:
            self._settle(admission, outcome)

    def _settle(self, reservation: Reservation, outcome: str) -> None:
        if outcome == "failure":
            return
        try:
            if outcome == "success":
                self._limiter.settle_success(reservation)
            else:
                self._limiter.release(reservation)
        except RateLimiterUnavailable:
            # The reservation expires with its window; never mask the real outcome.
            logger.warning("login_reservation_settle_failed", extra={"outcome": outcome})

    def _audit_login_denied(
        self,
        request_id: str | None,
        tenant_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        reason: str,
        tenant_slug: str,
    ) -> None:
        if tenant_id is None:
            with contextless_transaction(self._engine) as conn:
                record_audit_event(
                    conn,
                    request_id=request_id,
                    event_type="auth.login",
                    action="auth.login",
                    decision="deny",
                    reason=reason,
                    details={"tenant_slug": tenant_slug},
                )
            return
        with tenant_transaction(self._engine, tenant_id) as conn:
            record_audit_event(
                conn,
                request_id=request_id,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="auth.login",
                action="auth.login",
                decision="deny",
                reason=reason,
            )

    def _login_admitted(
        self, tenant_slug: str, email: str, password: str, request_id: str | None
    ) -> LoginResult:
        with contextless_transaction(self._engine) as conn:
            record = repo.lookup_login(conn, tenant_slug, email)

        # Always run one bounded hash verification so timing does not reveal
        # unknown accounts and memory use stays within the container budget.
        try:
            password_ok = self._verifier.verify(record.password_hash if record else None, password)
        except VerifierBusyError as exc:
            self._audit_login_denied(
                request_id,
                record.tenant_id if record else None,
                record.user_id if record else None,
                "verifier_busy",
                tenant_slug,
            )
            raise LoginBusy from exc

        reason: str | None = None
        if record is None:
            reason = "unknown_account"
        elif not password_ok:
            reason = "bad_password"
        elif not record.tenant_active:
            reason = "tenant_inactive"
        elif not record.user_active:
            reason = "user_inactive"

        if reason is not None or record is None:
            reason = reason or "unknown_account"
            self._audit_login_denied(
                request_id,
                record.tenant_id if record else None,
                record.user_id if record else None,
                reason,
                tenant_slug,
            )
            raise LoginFailed(reason)

        session_id = uuid.uuid4()
        now = datetime.now(UTC)
        token, expires_at = issue_access_token(
            secret=self._settings.jwt_secret.get_secret_value(),
            issuer=self._settings.jwt_issuer,
            audience=self._settings.jwt_audience,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            session_id=session_id,
            ttl_seconds=self._settings.access_token_ttl_seconds,
            now=now,
        )
        with tenant_transaction(self._engine, record.tenant_id, record.user_id) as conn:
            repo.create_session(
                conn,
                session_id=session_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                expires_at=expires_at,
            )
            record_audit_event(
                conn,
                request_id=request_id,
                tenant_id=record.tenant_id,
                user_id=record.user_id,
                event_type="auth.login",
                action="auth.login",
                decision="allow",
                reason="password_verified",
                details={"session_id": str(session_id)},
            )
        return LoginResult(access_token=token, expires_at=expires_at)

    # -- per-request authentication -----------------------------------------------

    def authenticate(self, token: str, request_id: str | None) -> AccessContext:
        """Validate the token, then load current state and permissions from PostgreSQL."""

        try:
            claims = decode_access_token(
                token,
                secret=self._settings.jwt_secret.get_secret_value(),
                issuer=self._settings.jwt_issuer,
                audience=self._settings.jwt_audience,
            )
        except InvalidTokenError as exc:
            with contextless_transaction(self._engine) as conn:
                record_audit_event(
                    conn,
                    request_id=request_id,
                    event_type="auth.token",
                    action="auth.authenticate",
                    decision="deny",
                    reason=f"token_{exc.reason}",
                )
            raise AuthenticationFailed(exc.reason) from exc

        # The tenant context comes from the verified signature, never from the request.
        with tenant_transaction(self._engine, claims.tenant_id, claims.user_id) as conn:
            session = repo.load_session(conn, claims.session_id, claims.user_id)
            reason: str | None = None
            if session is None:
                reason = "session_not_found"
            elif session.revoked:
                reason = "session_revoked"
            elif session.expired:
                reason = "session_expired"
            elif not session.tenant_active:
                reason = "tenant_inactive"
            elif not session.user_active:
                reason = "user_inactive"
            if reason is not None or session is None:
                record_audit_event(
                    conn,
                    request_id=request_id,
                    tenant_id=claims.tenant_id if session else None,
                    user_id=claims.user_id if session else None,
                    event_type="auth.token",
                    action="auth.authenticate",
                    decision="deny",
                    reason=reason or "session_not_found",
                )
                failure = reason or "session_not_found"
            else:
                caps = repo.load_role_capabilities(conn, session.user_id)
                groups = repo.load_groups(conn, session.user_id)
                failure = None

        if failure is not None or session is None:
            raise AuthenticationFailed(failure or "session_not_found")

        subject = Subject(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            active=True,
            capabilities=frozenset(
                CapabilityGrant(capability=cap, scope_department=scope)
                for _, cap, scope in caps
                if cap is not None
            ),
            group_ids=frozenset(gid for gid, _ in groups),
            clearance=session.clearance,
            country=session.country,
            department=session.department,
        )
        return AccessContext(
            request_id=request_id,
            session_id=session.session_id,
            session_expires_at=session.expires_at,
            tenant_slug=session.tenant_slug,
            tenant_name=session.tenant_name,
            policy_epoch=session.policy_epoch,
            email=session.email,
            display_name=session.display_name,
            role_keys=tuple(sorted({key for key, _, _ in caps})),
            group_keys=tuple(key for _, key in groups),
            subject=subject,
        )

    # -- logout ------------------------------------------------------------------

    def logout(self, ctx: AccessContext) -> None:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            repo.revoke_session(conn, ctx.session_id)
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="auth.logout",
                action="auth.logout",
                decision="info",
                reason="session_revoked",
                details={"session_id": str(ctx.session_id)},
            )
