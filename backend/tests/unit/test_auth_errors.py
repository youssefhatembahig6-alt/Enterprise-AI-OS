"""Review finding 1: every authentication failure raises a fresh exception instance."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import get_access_context
from app.api.errors import ApiError, unauthenticated
from app.services.identity import AuthenticationFailed


class _RejectingService:
    def authenticate(self, token: str, request_id: str | None) -> Any:
        raise AuthenticationFailed("bad_signature")


def _capture(credentials: HTTPAuthorizationCredentials | None) -> ApiError:
    with pytest.raises(ApiError) as info:
        get_access_context(_RejectingService(), credentials)  # type: ignore[arg-type]
    return info.value


def test_factory_returns_distinct_generic_instances() -> None:
    first, second = unauthenticated(), unauthenticated()
    assert first is not second
    for error in (first, second):
        assert (error.status_code, error.code, error.message) == (
            401,
            "unauthenticated",
            "Authentication required.",
        )
        assert error.headers == {"WWW-Authenticate": "Bearer"}
    first.headers["X-Mutated"] = "1"
    assert "X-Mutated" not in unauthenticated().headers


@pytest.mark.parametrize(
    "credentials",
    [None, HTTPAuthorizationCredentials(scheme="Bearer", credentials="forged")],
    ids=["missing", "rejected"],
)
def test_independent_failures_do_not_share_instances_or_tracebacks(
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    errors = [_capture(credentials) for _ in range(3)]
    assert len({id(e) for e in errors}) == 3
    for error in errors:
        assert error.status_code == 401
        assert error.headers == {"WWW-Authenticate": "Bearer"}
        # A fresh instance carries only its own raise: one dependency frame,
        # not an accumulating chain of earlier requests' frames.
        frames = []
        tb = error.__traceback__
        while tb is not None:
            frames.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        assert frames.count("get_access_context") == 1


def test_missing_and_rejected_failures_are_distinct() -> None:
    missing = _capture(None)
    rejected = _capture(HTTPAuthorizationCredentials(scheme="Bearer", credentials="x"))
    assert missing is not rejected
    assert isinstance(rejected.__cause__, AuthenticationFailed)
    assert missing.__cause__ is None
