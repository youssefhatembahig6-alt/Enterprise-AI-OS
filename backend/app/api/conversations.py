"""Chat history routes (M8): the signed-in employee's own conversations.

Every route is scoped to the session's employee and organization. A conversation that is
unknown, another employee's or another organization's returns the same 404, for opening
and for deleting alike. Answers are not stored here: each turn names its run, and the
client replays it with GET /api/v1/runs/{run_id}/events, which re-checks access.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import CurrentContext
from app.api.errors import ApiError
from app.chat.history import ChatHistory, ConversationNotFound, ConversationRow
from app.chat.routing import HANDLER_LABELS
from app.schemas.auth import ErrorResponse
from app.schemas.reports import ConversationDetail, ConversationList, ConversationOut, TurnOut

router = APIRouter(prefix="/api/v1/conversations", tags=["chat history"])
_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
}
HandlerName = Literal["general", "documents", "report", "legal", "code", "clarify"]


def get_history(request: Request) -> ChatHistory:
    history: ChatHistory = request.app.state.chat_history
    return history


History = Annotated[ChatHistory, Depends(get_history)]


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Not found.")


def _out(row: ConversationRow) -> ConversationOut:
    return ConversationOut(
        conversation_id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=ConversationList, responses=_ERRORS)
def list_conversations(ctx: CurrentContext, history: History) -> ConversationList:
    """The caller's own conversations, most recently used first."""

    return ConversationList(conversations=[_out(r) for r in history.conversations(ctx)])


@router.get("/{conversation_id}", response_model=ConversationDetail, responses=_ERRORS)
def get_conversation(
    conversation_id: UUID, ctx: CurrentContext, history: History
) -> ConversationDetail:
    try:
        row, turns = history.get(ctx, conversation_id)
    except ConversationNotFound as exc:
        raise _not_found() from exc
    return ConversationDetail(
        **_out(row).model_dump(),
        turns=[
            TurnOut(
                turn_id=t.id,
                question=t.question,
                handler=cast(HandlerName, t.handler),
                handler_label=HANDLER_LABELS[t.handler],
                run_id=t.run_id,
                reply=t.reply,
                created_at=t.created_at,
            )
            for t in turns
        ],
    )


@router.delete(
    "/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=_ERRORS,
)
def delete_conversation(conversation_id: UUID, ctx: CurrentContext, history: History) -> Response:
    """Delete one of the caller's conversations and its turns (audited)."""

    try:
        history.delete(ctx, conversation_id)
    except ConversationNotFound as exc:
        raise _not_found() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
