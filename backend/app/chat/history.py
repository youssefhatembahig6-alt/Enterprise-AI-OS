"""Private, persistent chat history (M8).

Every operation runs in a tenant transaction bound to the signed-in employee and filters
on ``owner_user_id`` explicitly; the database's RESTRICTIVE ``owner_only`` policy enforces
the same rule underneath (migration 0017). A conversation that does not exist, belongs to
another employee or to another organization is reported as ``ConversationNotFound``, and
the API answers all of them with the same 404.

Only the question, the chosen handler and the answering run's id are stored (plus the text
of a clarifying question, which has no run). Answers are replayed from the run, so the
employee's current access is checked again every time a conversation is reopened.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from app.db.engine import tenant_transaction
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext

MAX_LISTED = 200
TITLE_MAX = 80


class ConversationNotFound(Exception):
    """Unknown, foreign-employee and foreign-organization conversations alike."""


@dataclass(frozen=True, slots=True)
class ConversationRow:
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TurnRow:
    id: UUID
    question: str
    handler: str
    run_id: UUID | None
    reply: str | None
    created_at: datetime


def title_for(question: str) -> str:
    flat = " ".join(question.split())
    return flat if len(flat) <= TITLE_MAX else f"{flat[: TITLE_MAX - 1]}…"


def _owned(conn: Connection, ctx: AccessContext, conversation_id: UUID) -> ConversationRow:
    row = conn.execute(
        text(
            "SELECT id, title, created_at, updated_at FROM chat_conversations"
            " WHERE id = :id AND tenant_id = :t AND owner_user_id = :u"
        ),
        {"id": conversation_id, "t": ctx.tenant_id, "u": ctx.user_id},
    ).one_or_none()
    if row is None:
        raise ConversationNotFound
    return ConversationRow(row.id, row.title, row.created_at, row.updated_at)


class ChatHistory:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def conversations(self, ctx: AccessContext) -> list[ConversationRow]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            rows = conn.execute(
                text(
                    "SELECT id, title, created_at, updated_at FROM chat_conversations"
                    " WHERE tenant_id = :t AND owner_user_id = :u"
                    " ORDER BY updated_at DESC, id LIMIT :n"
                ),
                {"t": ctx.tenant_id, "u": ctx.user_id, "n": MAX_LISTED},
            ).all()
        return [ConversationRow(r.id, r.title, r.created_at, r.updated_at) for r in rows]

    def get(
        self, ctx: AccessContext, conversation_id: UUID
    ) -> tuple[ConversationRow, list[TurnRow]]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id, snapshot=True) as conn:
            conversation = _owned(conn, ctx, conversation_id)
            rows = conn.execute(
                text(
                    "SELECT id, question, handler, run_id, reply, created_at FROM chat_turns"
                    " WHERE tenant_id = :t AND owner_user_id = :u AND conversation_id = :c"
                    " ORDER BY created_at, id"
                ),
                {"t": ctx.tenant_id, "u": ctx.user_id, "c": conversation_id},
            ).all()
        turns = [
            TurnRow(r.id, r.question, r.handler, r.run_id, r.reply, r.created_at) for r in rows
        ]
        return conversation, turns

    def require_owned(self, ctx: AccessContext, conversation_id: UUID) -> None:
        """Raise ``ConversationNotFound`` before any run is created for a foreign id."""

        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            _owned(conn, ctx, conversation_id)

    def record_turn(
        self,
        ctx: AccessContext,
        conversation_id: UUID | None,
        question: str,
        handler: str,
        *,
        run_id: UUID | None = None,
        reply: str | None = None,
    ) -> UUID | None:
        """Append one exchange; create the conversation first when ``conversation_id`` is
        None. Returns the conversation id, or None when the given conversation is no longer
        the employee's (deleted meanwhile): the turn is then not stored.

        Idempotent per run: retrying a request that returned the same run adds nothing.
        """

        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            if run_id is not None:
                existing = conn.execute(
                    text(
                        "SELECT conversation_id FROM chat_turns"
                        " WHERE tenant_id = :t AND owner_user_id = :u AND run_id = :r"
                    ),
                    {"t": ctx.tenant_id, "u": ctx.user_id, "r": run_id},
                ).scalar_one_or_none()
                if existing is not None:
                    return UUID(str(existing))
            if conversation_id is None:
                conversation_id = uuid.uuid4()
                conn.execute(
                    text(
                        "INSERT INTO chat_conversations (id, tenant_id, owner_user_id, title)"
                        " VALUES (:id, :t, :u, :title)"
                    ),
                    {
                        "id": conversation_id,
                        "t": ctx.tenant_id,
                        "u": ctx.user_id,
                        "title": title_for(question),
                    },
                )
            else:
                touched = conn.execute(
                    text(
                        "UPDATE chat_conversations SET updated_at = clock_timestamp()"
                        " WHERE id = :id AND tenant_id = :t AND owner_user_id = :u"
                    ),
                    {"id": conversation_id, "t": ctx.tenant_id, "u": ctx.user_id},
                ).rowcount
                if touched != 1:
                    return None
            conn.execute(
                text(
                    "INSERT INTO chat_turns (id, tenant_id, owner_user_id, conversation_id,"
                    " question, handler, run_id, reply)"
                    " VALUES (:id, :t, :u, :c, :q, :h, :r, :reply)"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": ctx.tenant_id,
                    "u": ctx.user_id,
                    "c": conversation_id,
                    "q": question,
                    "h": handler,
                    "r": run_id,
                    "reply": reply,
                },
            )
        return conversation_id

    def delete(self, ctx: AccessContext, conversation_id: UUID) -> None:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            deleted = conn.execute(
                text(
                    "DELETE FROM chat_conversations"
                    " WHERE id = :id AND tenant_id = :t AND owner_user_id = :u"
                ),
                {"id": conversation_id, "t": ctx.tenant_id, "u": ctx.user_id},
            ).rowcount
            if deleted != 1:
                raise ConversationNotFound
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="chat.history",
                action="chat.conversation.delete",
                decision="allow",
                reason="owner_deleted",
                details={"conversation_id": str(conversation_id)},
            )
