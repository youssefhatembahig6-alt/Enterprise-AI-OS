"""General mode (M8): one question answered by the chat model alone.

The model receives only the system prompt below and the user's question. It gets no
document passages, no SQL results, no email or briefing tools and no earlier turns, so a
general reply cannot disclose company data the user may not see. It is published as the
model's own, unverified reply (``general_model_reply_unverified``) and the web UI labels
it as general. The run is still a normal run: tenant pack switch (``general``) checked at
start and at publication, owner-only replay, cancellation, typed model failures and, in
gateway mode, the pinned model and revision checked against the gateway's ``/health``
before the call (recorded in the run's diagnostics).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph

from app.config import ModelMode
from app.reporting import store as runs
from app.reporting.contracts import (
    TERMINAL_STATUSES,
    ErrorCode,
    EventType,
    Outcome,
    RunStatus,
    Step,
    VerificationLabel,
)
from app.reporting.model_client import FAKE_LABEL
from app.reporting.workflow import ReportWorkflow
from app.tenancy.config import pack_enabled

GENERAL_MAX_TOKENS = 700
PUBLISHED_LABEL = "Published the general reply"

SYSTEM_PROMPT = (
    "You are the general assistant of an enterprise AI system. In this mode you have NO "
    "access to the company's documents, databases, sales figures, email or any tools, and "
    "you must not claim or imply such access. Answer from general knowledge only. If the "
    "question needs company-specific information (policies, documents, figures, people), "
    "say briefly that general mode cannot see company data and suggest asking again with "
    "the Documents or Sales report source. Be accurate, concise and practical. Plain text; "
    "short paragraphs or simple lists are fine."
)


class GeneralState(TypedDict, total=False):
    run_id: UUID
    tenant_id: UUID
    user_id: UUID
    question: str
    inference_mode: str
    terminal: dict[str, Any] | None
    reply: str


def messages(question: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]


def paragraphs(text: str) -> list[str]:
    """Split the reply into paragraphs for streaming; keeps list lines together."""

    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


class GeneralAnswerWorkflow(ReportWorkflow):
    kind = "general"

    def _build(self) -> Any:
        graph: StateGraph[GeneralState] = StateGraph(GeneralState)
        nodes = {
            "prepare": self.general_prepare,
            "answer": self.answer,
            "publish": self.general_publish,
            "finish": self.finish,
        }
        for name, fn in nodes.items():
            graph.add_node(name, self._wrap(name, fn))  # type: ignore[call-overload]
        graph.add_edge(START, "prepare")
        graph.add_conditional_edges("prepare", self._next_or_finish("answer"))
        graph.add_conditional_edges("answer", self._next_or_finish("publish"))
        graph.add_edge("publish", "finish")
        graph.add_edge("finish", END)
        return graph.compile()

    def _final_stats(self, state: Any) -> dict[str, Any]:
        return dict(self._stats_for(state))

    # -- nodes ---------------------------------------------------------------------

    def general_prepare(self, state: GeneralState) -> dict[str, Any]:
        with self._tx(state) as conn:
            run = runs.load_run(conn, state["run_id"])
            if run is None or run.status in TERMINAL_STATUSES:
                return {"terminal": {"kind": "noop"}}
            runs.update_run(conn, run.id, status=RunStatus.RUNNING)
            if not pack_enabled(conn, self.kind):
                return {"terminal": self._disabled("start")}
        self._status(state, Step.AUTHORIZATION, "completed")
        stats = self._stats_for(state)
        stats["model"] = self.deps.model.describe()
        stats["grounding"] = "none (general mode: no documents, data or actions)"
        if self.deps.model.mode is ModelMode.GATEWAY:
            # Refuses anything but the pinned chat model and revision (typed failure).
            stats["served_models"] = self.deps.model.served_models(self._token(state)).summary()
        return {}

    def answer(self, state: GeneralState) -> dict[str, Any]:
        result = self.deps.model.chat(
            messages(state["question"]),
            purpose="general",
            max_tokens=GENERAL_MAX_TOKENS,
            cancel=self._token(state),
        )
        self._stat(
            state,
            "model_calls",
            {
                "purpose": "general",
                "seconds": round(result.total_seconds, 3),
                "ttft": result.ttft_seconds,
                "tokens": result.completion_tokens,
                "finish_reason": result.finish_reason,
            },
        )
        text = result.text.strip()
        if not text:
            return {
                "terminal": {
                    "kind": "fail",
                    "code": ErrorCode.MODEL_UNAVAILABLE,
                    "private": "empty_reply",
                }
            }
        if result.finish_reason == "length":
            text += "\n\n[The reply reached the length limit and was cut off.]"
        self._status(state, Step.GENERAL_ANSWER, "completed")
        return {"reply": text}

    def verification(self, state: GeneralState) -> dict[str, Any]:
        mode = state["inference_mode"]
        verification: dict[str, Any] = {
            "label": VerificationLabel.GENERAL_UNVERIFIED.value,
            "grounding": "none",
            "company_data": "not_used",
            "checked_against_sources": False,
            "inference_mode": mode,
            "checker": None,
        }
        served = self._stats_for(state).get("served_models") or {}
        chat = (served.get("models") or {}).get("chat") if isinstance(served, dict) else None
        if isinstance(chat, dict):
            verification["model"] = {"name": chat.get("model"), "revision": chat.get("revision")}
        if mode == ModelMode.FAKE.value:
            verification["inference_label"] = FAKE_LABEL
        return verification

    def general_publish(self, state: GeneralState) -> dict[str, Any]:
        message_id = uuid.uuid4()
        mode = state["inference_mode"]
        parts = paragraphs(state["reply"])
        verification = self.verification(state)
        result = {
            "message_id": str(message_id),
            "kind": self.kind,
            "answer": [{"text": p, "citations": []} for p in parts],
            "citations": [],
            "verification": verification,
        }
        with self._tx(state) as conn:
            run = runs.lock_run(conn, state["run_id"])
            if run is None or run.status in TERMINAL_STATUSES:
                return {"terminal": {"kind": "noop"}}
            if run.cancel_requested:
                return {"terminal": {"kind": "cancel", "code": ErrorCode.CANCELLED}}
            if not pack_enabled(conn, self.kind):
                return {"terminal": self._disabled("publication")}
            rid, tid = state["run_id"], state["tenant_id"]
            label = {"inference_label": FAKE_LABEL} if mode == ModelMode.FAKE.value else {}
            runs.append_event(
                conn,
                rid,
                tid,
                EventType.STATUS,
                {
                    "step": Step.PUBLICATION.value,
                    "state": "completed",
                    "label": PUBLISHED_LABEL,
                    "inference": mode,
                    **label,
                },
            )
            for index, part in enumerate(parts):
                runs.append_event(
                    conn,
                    rid,
                    tid,
                    EventType.TOKEN,
                    {
                        "message_id": str(message_id),
                        "part": "text",
                        "index": index,
                        "text": part,
                        "citations": [],
                    },
                )
            runs.append_event(
                conn,
                rid,
                tid,
                EventType.DONE,
                {
                    "message_id": str(message_id),
                    "outcome": Outcome.PUBLISHED.value,
                    "verification": verification,
                    "abstention": None,
                    "inference": mode,
                },
            )
            self._audit(
                conn,
                state,
                event_type="general.publish",
                action="model.general",
                decision="info",
                reason="published",
                details={"run_id": str(rid), "inference": mode},
            )
            runs.update_run(
                conn,
                rid,
                status=RunStatus.PUBLISHED,
                result=json.loads(json.dumps(result)),
                outcome_code=verification["label"],
                stats=self._final_stats(state),
                finished_at="now",
            )
            runs.finish_job(conn, rid)
        return {"terminal": {"kind": "published"}}


__all__ = ["GeneralAnswerWorkflow", "messages", "paragraphs"]
