"""Desktop local-folder routes (M5, ADR 0503). The signed-in user owns the folders
they register; another user's folder is reported as not found. The backend assigns
department, classification and sharing; the desktop app only pushes file events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response

from app.api.admin import capability_disabled
from app.api.deps import CurrentContext
from app.api.errors import ApiError
from app.connectors.local_folder import Event, FolderInvalid, FolderNotFound, LocalFolderService
from app.schemas.auth import ErrorResponse
from app.schemas.local_folders import EventsIn, FolderIn, SharingIn
from app.tenancy.config import CapabilityDisabled

router = APIRouter(prefix="/api/v1/local-folders", tags=["local-folders"])
_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def get_folders(request: Request) -> LocalFolderService:
    service: LocalFolderService = request.app.state.local_folders
    return service


Folders = Annotated[LocalFolderService, Depends(get_folders)]


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except FolderNotFound as exc:
        raise ApiError(404, "not_found", "Not found.") from exc
    except FolderInvalid as exc:
        raise ApiError(422, exc.code, exc.message) from exc
    except CapabilityDisabled as exc:
        raise capability_disabled(exc) from exc


@router.post("", status_code=status.HTTP_201_CREATED, responses=_ERRORS)
def register(body: FolderIn, ctx: CurrentContext, folders: Folders) -> dict[str, Any]:
    """Register a folder owned by the caller (idempotent per ``folder_id``)."""

    return _call(lambda: folders.register(ctx, body.folder_id, body.name, body.device))


@router.get("", responses=_ERRORS)
def list_folders(ctx: CurrentContext, folders: Folders) -> list[dict[str, Any]]:
    return _call(lambda: folders.list_own(ctx))


@router.put("/{source_id}/sharing", responses=_ERRORS)
def set_sharing(
    source_id: UUID, body: SharingIn, ctx: CurrentContext, folders: Folders
) -> dict[str, Any]:
    """Owner only; groups the owner belongs to."""

    return _call(lambda: folders.set_sharing(ctx, source_id, body.groups))


@router.get("/{source_id}/items", responses=_ERRORS)
def items(source_id: UUID, ctx: CurrentContext, folders: Folders) -> list[dict[str, Any]]:
    """Paths the server holds for this folder (owner only), for reconciliation."""

    return _call(lambda: folders.items(ctx, source_id))


@router.post("/{source_id}/events", responses=_ERRORS)
def events(
    source_id: UUID, body: EventsIn, ctx: CurrentContext, folders: Folders
) -> dict[str, Any]:
    """Apply a batch of file events. Each event is applied once (by ``event_id``); the
    response lists a result per event in order."""

    batch = [
        Event(
            event_id=e.event_id,
            seq=e.seq,
            op=e.op,
            path=e.path,
            sha256=e.sha256,
            size=e.size,
            content_base64=e.content_base64,
        )
        for e in body.events
    ]
    return {"results": _call(lambda: folders.apply_events(ctx, source_id, batch))}


@router.delete("/{source_id}", status_code=204, response_class=Response, responses=_ERRORS)
def delete(source_id: UUID, ctx: CurrentContext, folders: Folders) -> Response:
    """Owner removes the folder; every document in it is tombstoned."""

    _call(lambda: folders.delete(ctx, source_id))
    return Response(status_code=204)
