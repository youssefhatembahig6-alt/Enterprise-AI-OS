"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.errors import unauthenticated
from app.logging_setup import request_id_var
from app.security.context import AccessContext
from app.services.identity import AuthenticationFailed, IdentityService

_bearer = HTTPBearer(auto_error=False)


def get_identity_service(request: Request) -> IdentityService:
    service: IdentityService = request.app.state.identity
    return service


def get_access_context(
    service: Annotated[IdentityService, Depends(get_identity_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AccessContext:
    """Build the access context from the bearer token and current database state.

    Tenant, role, group, country and clearance values in headers, query strings or
    bodies are never read here.
    """

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthenticated()
    try:
        return service.authenticate(credentials.credentials, request_id_var.get())
    except AuthenticationFailed as exc:
        raise unauthenticated() from exc


CurrentContext = Annotated[AccessContext, Depends(get_access_context)]
