"""M6 action routes: email drafts from verified reports, human approval, send through
the outbox, cancellation, and review-only briefing schedules.

Foreign, unknown and other tenants' drafts, approvals and schedules return the same 404
or the same generic approval rejection. Identity always comes from the session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.actions.contracts import (
    ApprovalOut,
    DraftCreate,
    DraftEdit,
    DraftOut,
    RecipientOut,
    ScheduleCreate,
    ScheduleOut,
    SendRequest,
    VersionRef,
)
from app.actions.service import ActionError, ActionService, Denied
from app.api.deps import CurrentContext
from app.api.errors import ApiError
from app.api.reports import choose_kind
from app.schemas.auth import ErrorResponse
from app.security.context import AccessContext
from app.tenancy.config import CapabilityDisabled

router = APIRouter(prefix="/api/v1/actions", tags=["actions"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def get_action_service(request: Request) -> ActionService:
    service: ActionService = request.app.state.actions
    return service


Service = Annotated[ActionService, Depends(get_action_service)]


def _api(error: ActionError) -> ApiError:
    return ApiError(error.status, error.code, error.message)


def _call[T](service: ActionService, ctx: AccessContext, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except Denied as denied:
        service.record_denial(ctx, denied)
        raise _api(denied.error) from None
    except ActionError as exc:
        raise _api(exc) from None


@router.get("/recipients", response_model=list[RecipientOut], responses=_ERRORS)
def recipients(ctx: CurrentContext, service: Service) -> list[RecipientOut]:
    """Active users of the caller's organization (the only recipients allowed)."""

    return _call(service, ctx, lambda: service.recipients(ctx))


@router.post(
    "/email-drafts",
    response_model=DraftOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_draft(body: DraftCreate, ctx: CurrentContext, service: Service) -> DraftOut:
    """Draft an email from one of the caller's published verified reports."""

    return _call(service, ctx, lambda: service.create_draft(ctx, body))


@router.get("/email-drafts", response_model=list[DraftOut], responses=_ERRORS)
def list_drafts(ctx: CurrentContext, service: Service) -> list[DraftOut]:
    return _call(service, ctx, lambda: service.list_drafts(ctx))


@router.get("/email-drafts/{action_id}", response_model=DraftOut, responses=_ERRORS)
def get_draft(action_id: UUID, ctx: CurrentContext, service: Service) -> DraftOut:
    return _call(service, ctx, lambda: service.get_draft(ctx, action_id))


@router.put("/email-drafts/{action_id}", response_model=DraftOut, responses=_ERRORS)
def edit_draft(action_id: UUID, body: DraftEdit, ctx: CurrentContext, service: Service) -> DraftOut:
    """Save an edit as a new immutable version. Any earlier approval stops being valid."""

    return _call(service, ctx, lambda: service.edit(ctx, action_id, body))


@router.post("/email-drafts/{action_id}/approve", response_model=ApprovalOut, responses=_ERRORS)
def approve_draft(
    action_id: UUID, body: VersionRef, ctx: CurrentContext, service: Service
) -> ApprovalOut:
    """Approve the exact version and hash the caller reviewed. Returns a one-time,
    short-lived approval token bound to this user, session, tenant and version."""

    return _call(service, ctx, lambda: service.approve(ctx, action_id, body))


@router.post(
    "/email-drafts/{action_id}/send",
    response_model=DraftOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_ERRORS,
)
def send_draft(
    action_id: UUID, body: SendRequest, ctx: CurrentContext, service: Service
) -> DraftOut:
    """Consume the approval and queue the email in the durable outbox."""

    return _call(service, ctx, lambda: service.send(ctx, action_id, body))


@router.post("/email-drafts/{action_id}/cancel", response_model=DraftOut, responses=_ERRORS)
def cancel_draft(action_id: UUID, ctx: CurrentContext, service: Service) -> DraftOut:
    return _call(service, ctx, lambda: service.cancel(ctx, action_id))


@router.post(
    "/briefings",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
)
def create_briefing(body: ScheduleCreate, ctx: CurrentContext, service: Service) -> ScheduleOut:
    """Schedule a recurring briefing. Each run produces a draft for review; nothing is
    ever sent without a separate human approval."""

    def kind_of(question: str) -> str:
        try:
            return choose_kind(question, "auto", None)
        except CapabilityDisabled:
            return "unsupported"

    return _call(service, ctx, lambda: service.create_schedule(ctx, body, kind_of))


@router.get("/briefings", response_model=list[ScheduleOut], responses=_ERRORS)
def list_briefings(ctx: CurrentContext, service: Service) -> list[ScheduleOut]:
    return _call(service, ctx, lambda: service.list_schedules(ctx))


@router.post("/briefings/{schedule_id}/deactivate", response_model=ScheduleOut, responses=_ERRORS)
def deactivate_briefing(schedule_id: UUID, ctx: CurrentContext, service: Service) -> ScheduleOut:
    return _call(service, ctx, lambda: service.deactivate_schedule(ctx, schedule_id))
