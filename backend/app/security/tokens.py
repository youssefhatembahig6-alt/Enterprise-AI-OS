"""Access-token (JWT) issuing and strict validation.

See docs/adr/0101-identity-and-session.md. The token proves identity only; current
permissions are always reloaded from PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "tenant_id", "jti", "iat", "exp", "iss", "aud")


class InvalidTokenError(Exception):
    """Raised for any token problem. The private reason is for audit only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: UUID
    tenant_id: UUID
    session_id: UUID
    issued_at: datetime
    expires_at: datetime


def issue_access_token(
    *,
    secret: str,
    issuer: str,
    audience: str,
    user_id: UUID,
    tenant_id: UUID,
    session_id: UUID,
    ttl_seconds: int,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    expires = issued + timedelta(seconds=ttl_seconds)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "jti": str(session_id),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": issuer,
        "aud": audience,
    }
    token = jwt.encode(payload, secret, algorithm=ALGORITHM, headers={"typ": "JWT"})
    return token, expires


def _uuid_claim(payload: dict[str, object], name: str) -> UUID:
    value = payload.get(name)
    if not isinstance(value, str):
        raise InvalidTokenError(f"claim_{name}_invalid")
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidTokenError(f"claim_{name}_invalid") from exc


def decode_access_token(token: str, *, secret: str, issuer: str, audience: str) -> TokenClaims:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("malformed") from exc
    if header.get("alg") != ALGORITHM:
        raise InvalidTokenError("algorithm_not_allowed")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=issuer,
            audience=audience,
            options={"require": list(REQUIRED_CLAIMS), "verify_signature": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("expired") from exc
    except jwt.InvalidSignatureError as exc:
        raise InvalidTokenError("bad_signature") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise InvalidTokenError(f"missing_claim_{exc.claim}") from exc
    except jwt.InvalidIssuerError as exc:
        raise InvalidTokenError("bad_issuer") from exc
    except jwt.InvalidAudienceError as exc:
        raise InvalidTokenError("bad_audience") from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("invalid") from exc

    iat, exp = payload.get("iat"), payload.get("exp")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise InvalidTokenError("claim_time_invalid")
    return TokenClaims(
        user_id=_uuid_claim(payload, "sub"),
        tenant_id=_uuid_claim(payload, "tenant_id"),
        session_id=_uuid_claim(payload, "jti"),
        issued_at=datetime.fromtimestamp(iat, UTC),
        expires_at=datetime.fromtimestamp(exp, UTC),
    )
