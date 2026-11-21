"""Token validation edge cases and log redaction (no database needed)."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import jwt
import pytest

from app.logging_setup import JsonFormatter, redact_text
from app.security.tokens import InvalidTokenError, decode_access_token, issue_access_token

SECRET = "unit-test-secret-" + "x" * 32
ISS, AUD = "eaios-api", "eaios-web"


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": ISS,
        "aud": AUD,
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def _decode(token: str) -> None:
    decode_access_token(token, secret=SECRET, issuer=ISS, audience=AUD)


def test_roundtrip() -> None:
    uid, tid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token, _ = issue_access_token(
        secret=SECRET,
        issuer=ISS,
        audience=AUD,
        user_id=uid,
        tenant_id=tid,
        session_id=sid,
        ttl_seconds=600,
    )
    claims = decode_access_token(token, secret=SECRET, issuer=ISS, audience=AUD)
    assert (claims.user_id, claims.tenant_id, claims.session_id) == (uid, tid, sid)


@pytest.mark.parametrize("claim", ["sub", "tenant_id", "jti", "iat", "exp", "iss", "aud"])
def test_missing_required_claim(claim: str) -> None:
    token = jwt.encode(_claims(**{claim: None}), SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        _decode(token)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int((datetime.now(UTC) - timedelta(seconds=5)).timestamp())}, "expired"),
        ({"iss": "someone-else"}, "bad_issuer"),
        ({"aud": "another-app"}, "bad_audience"),
        ({"sub": "not-a-uuid"}, "claim_sub_invalid"),
        ({"tenant_id": 42}, "claim_tenant_id_invalid"),
    ],
)
def test_invalid_claims(overrides: dict[str, object], reason: str) -> None:
    token = jwt.encode(_claims(**overrides), SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError) as info:
        _decode(token)
    assert info.value.reason == reason


def test_wrong_signature() -> None:
    token = jwt.encode(_claims(), "another-secret-" + "y" * 32, algorithm="HS256")
    with pytest.raises(InvalidTokenError) as info:
        _decode(token)
    assert info.value.reason == "bad_signature"


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_other_hmac_algorithms_rejected(alg: str) -> None:
    token = jwt.encode(_claims(), SECRET, algorithm=alg)
    with pytest.raises(InvalidTokenError) as info:
        _decode(token)
    assert info.value.reason == "algorithm_not_allowed"


def test_alg_none_rejected() -> None:
    token = jwt.encode(_claims(), cast(Any, None), algorithm="none")
    with pytest.raises(InvalidTokenError) as info:
        _decode(token)
    assert info.value.reason == "algorithm_not_allowed"


def test_garbage_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        _decode("not.a.jwt")


def test_redaction_of_bearer_jwt_and_url_passwords() -> None:
    token = jwt.encode(_claims(), SECRET, algorithm="HS256")
    text = f"Authorization: Bearer {token} postgresql://app:hunter2secret@db/x raw {token}"
    redacted = redact_text(text)
    assert token not in redacted
    assert "hunter2secret" not in redacted


def test_json_formatter_redacts_sensitive_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"msg": "login", "password": "pw-value-123", "jwt_secret": "s3cr3t", "user": "u1"}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["password"] == "[REDACTED]"
    assert payload["jwt_secret"] == "[REDACTED]"
    assert payload["user"] == "u1"
