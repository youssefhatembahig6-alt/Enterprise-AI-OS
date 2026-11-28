"""M8 automatic chat routing and private chat history, on real PostgreSQL through the API.

Fake inference, inline dispatch and the NileTech sales import (the M2 report harness):
General replies, verified SQL reports and clarifying questions run end to end. Document
answers need Qdrant and are covered by the document suites; routing to documents is
unit-tested in tests/unit/test_chat_routing.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.engine import tenant_transaction
from app.main import create_app
from tests.integration.conftest import DbHarness, login, owner_exec, report_settings
from tests.integration.document_support import auth
from tests.integration.report_support import Frame, parse_sse, replay

FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
OTHER_ORG = "isolation-test/finance-reader"
SALES_Q = "What were fulfilled sales in Q1 2026?"


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    frames: list[Frame]

    def of(self, event: str) -> list[Frame]:
        return [f for f in self.frames if f.event == event]

    @property
    def done(self) -> dict[str, Any]:
        frames = self.of("done")
        assert frames, self.frames
        payload: dict[str, Any] = frames[-1].data
        return payload

    @property
    def first_status(self) -> dict[str, Any]:
        payload: dict[str, Any] = self.of("status")[0].data
        return payload


def chat(client: TestClient, token: str, question: str, **body: Any) -> Reply:
    with client.stream(
        "POST", "/api/v1/chat", json={"question": question, **body}, headers=auth(token)
    ) as response:
        raw = "".join(response.iter_text())
        headers = {k.lower(): v for k, v in response.headers.items()}
        return Reply(response.status_code, headers, parse_sse(raw) if raw else [])


@pytest.fixture(scope="module")
def client(report_db: DbHarness, stub_http_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(report_settings(report_db, stub_http_url))) as c:
        yield c


@pytest.fixture
def token(client: TestClient, report_db: DbHarness) -> Callable[[str], str]:
    return lambda ref: login(client, report_db, ref)


def run_count(db: DbHarness) -> int:
    count: int = owner_exec(db, "SELECT count(*) FROM report_runs").scalar_one()
    return count


def test_hello_gets_a_general_reply_labelled_general(
    client: TestClient, token: Callable[[str], str]
) -> None:
    reply = chat(client, token(FINANCE), "hello")
    assert reply.status == 200
    assert reply.headers["x-handler"] == "general"
    assert reply.first_status["handler"] == "general"
    assert reply.first_status["handler_label"] == "General"
    assert reply.first_status["route_reason"] == "greeting"
    assert reply.done["outcome"] == "published"
    assert reply.done["verification"]["label"] == "general_model_reply_unverified"
    assert reply.headers["x-conversation-id"]


def test_a_sales_question_routes_to_the_verified_sql_report(
    client: TestClient, token: Callable[[str], str]
) -> None:
    reply = chat(client, token(FINANCE), SALES_Q)
    assert reply.headers["x-handler"] == "report"
    assert reply.first_status["handler_label"] == "Sales report (SQL)"
    assert reply.done["outcome"] == "published"
    assert reply.done["verification"]["label"].startswith("figures_verified_")
    assert reply.of("citation"), "the report cites its SQL evidence"


def test_a_vague_request_gets_a_clarifying_question_and_no_run(
    client: TestClient, token: Callable[[str], str], report_db: DbHarness
) -> None:
    before = run_count(report_db)
    reply = chat(client, token(FINANCE), "the numbers?")
    assert reply.status == 200
    assert reply.headers["x-handler"] == "clarify"
    assert "x-run-id" not in reply.headers
    assert reply.done["abstention"]["code"] == "clarification_needed"
    assert "sales figures" in reply.done["abstention"]["message"]
    assert run_count(report_db) == before
    conversation = reply.headers["x-conversation-id"]
    detail = client.get(f"/api/v1/conversations/{conversation}", headers=auth(token(FINANCE)))
    [turn] = detail.json()["turns"]
    assert turn["handler"] == "clarify" and turn["run_id"] is None
    assert turn["reply"] == reply.done["abstention"]["message"]


def test_explicit_modes_still_run_their_handler(
    client: TestClient, token: Callable[[str], str]
) -> None:
    reply = chat(client, token(FINANCE), "hello", mode="report")
    assert reply.headers["x-handler"] == "report"
    assert reply.done["abstention"]["code"] == "clarification_needed"  # the report's own


def test_conversations_persist_and_reopen_through_the_run(
    client: TestClient, token: Callable[[str], str]
) -> None:
    first = chat(client, token(FINANCE), "Give me one tip for clear emails.")
    conversation = first.headers["x-conversation-id"]
    second = chat(client, token(FINANCE), SALES_Q, conversation_id=conversation)
    assert second.headers["x-conversation-id"] == conversation

    # A fresh sign-in (new token) still sees it: history lives on the server.
    fresh = token(FINANCE)
    listed = client.get("/api/v1/conversations", headers=auth(fresh)).json()["conversations"]
    mine = next(c for c in listed if c["conversation_id"] == conversation)
    assert mine["title"] == "Give me one tip for clear emails."
    assert listed[0]["conversation_id"] == conversation  # most recently used first

    detail = client.get(f"/api/v1/conversations/{conversation}", headers=auth(fresh)).json()
    assert [(t["question"], t["handler"], t["handler_label"]) for t in detail["turns"]] == [
        ("Give me one tip for clear emails.", "general", "General"),
        (SALES_Q, "report", "Sales report (SQL)"),
    ]
    assert detail["turns"][1]["run_id"] == second.headers["x-run-id"]
    # The answer is replayed from the run, with the handler label in its first event.
    again = replay(client, fresh, detail["turns"][1]["run_id"])
    assert again.status_code == 200
    assert again.of("status")[0].data["handler"] == "report"
    assert again.done["outcome"] == "published"


def test_retrying_a_request_adds_no_second_turn(
    client: TestClient, token: Callable[[str], str]
) -> None:
    key = "retry-" + uuid.uuid4().hex[:12]
    first = chat(client, token(FINANCE), "hello", client_request_id=key)
    conversation = first.headers["x-conversation-id"]
    again = chat(
        client, token(FINANCE), "hello", client_request_id=key, conversation_id=conversation
    )
    assert again.headers["x-run-id"] == first.headers["x-run-id"]
    detail = client.get(f"/api/v1/conversations/{conversation}", headers=auth(token(FINANCE)))
    assert len(detail.json()["turns"]) == 1


def test_another_employee_cannot_list_open_append_to_or_delete_it(
    client: TestClient, token: Callable[[str], str], report_db: DbHarness
) -> None:
    owner = token(FINANCE)
    conversation = chat(client, owner, "hello").headers["x-conversation-id"]
    for intruder_ref in (SALES, OTHER_ORG):
        intruder = token(intruder_ref)
        listed = client.get("/api/v1/conversations", headers=auth(intruder)).json()
        assert conversation not in {c["conversation_id"] for c in listed["conversations"]}
        opened = client.get(f"/api/v1/conversations/{conversation}", headers=auth(intruder))
        assert opened.status_code == 404
        assert opened.json()["error"]["code"] == "not_found"
        before = run_count(report_db)
        appended = chat(client, intruder, "hello", conversation_id=conversation)
        assert appended.status == 404
        assert run_count(report_db) == before, "no run for a foreign conversation"
        deleted = client.delete(f"/api/v1/conversations/{conversation}", headers=auth(intruder))
        assert deleted.status_code == 404
    # Unknown ids look the same as foreign ones.
    unknown = client.get(f"/api/v1/conversations/{uuid.uuid4()}", headers=auth(owner))
    assert unknown.status_code == 404
    # The owner still has it, unchanged.
    detail = client.get(f"/api/v1/conversations/{conversation}", headers=auth(owner)).json()
    assert len(detail["turns"]) == 1
    assert client.get("/api/v1/conversations").status_code == 401


def test_the_owner_deletes_a_conversation_and_it_stays_gone(
    client: TestClient, token: Callable[[str], str], report_db: DbHarness
) -> None:
    owner = token(FINANCE)
    reply = chat(client, owner, "hello")
    conversation = reply.headers["x-conversation-id"]
    assert (
        client.delete(f"/api/v1/conversations/{conversation}", headers=auth(owner)).status_code
        == 204
    )
    fresh = token(FINANCE)
    listed = client.get("/api/v1/conversations", headers=auth(fresh)).json()["conversations"]
    assert conversation not in {c["conversation_id"] for c in listed}
    assert (
        client.get(f"/api/v1/conversations/{conversation}", headers=auth(fresh)).status_code == 404
    )
    assert (
        client.delete(f"/api/v1/conversations/{conversation}", headers=auth(fresh)).status_code
        == 404
    )
    turns = owner_exec(
        report_db,
        "SELECT count(*) FROM chat_turns WHERE conversation_id = :c",
        {"c": conversation},
    ).scalar_one()
    assert turns == 0
    audit = owner_exec(
        report_db,
        "SELECT count(*) FROM audit_events WHERE action = 'chat.conversation.delete'"
        " AND details->>'conversation_id' = :c",
        {"c": conversation},
    ).scalar_one()
    assert audit == 1
    # Appending to a deleted conversation is refused like a foreign one.
    assert chat(client, fresh, "hello", conversation_id=conversation).status == 404


def test_the_database_enforces_owner_only_history(
    client: TestClient, token: Callable[[str], str], report_db: DbHarness
) -> None:
    """Even a query without an owner filter sees and changes only the session's rows."""

    conversation = chat(client, token(FINANCE), "hello").headers["x-conversation-id"]
    tenant = report_db.foundation.tenants["niletech"]
    sales = tenant.users["sales-reader"].id
    finance = tenant.users["finance-reader"].id
    engine = client.app.state.engine  # type: ignore[attr-defined]
    with tenant_transaction(engine, tenant.id, sales) as conn:
        assert conn.execute(text("SELECT count(*) FROM chat_conversations")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM chat_turns")).scalar_one() == 0
        gone = conn.execute(
            text("DELETE FROM chat_conversations WHERE id = :c"), {"c": conversation}
        ).rowcount
        assert gone == 0
    with (
        pytest.raises(Exception, match="row-level security"),
        tenant_transaction(engine, tenant.id, sales) as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO chat_conversations (id, tenant_id, owner_user_id, title)"
                " VALUES (:id, :t, :u, 'planted')"
            ),
            {"id": uuid.uuid4(), "t": tenant.id, "u": finance},
        )
    with tenant_transaction(engine, tenant.id, finance) as conn:
        mine = conn.execute(
            text("SELECT count(*) FROM chat_conversations WHERE id = :c"), {"c": conversation}
        ).scalar_one()
        assert mine == 1


@pytest.fixture
def revoke_finance_team(report_db: DbHarness) -> Iterator[Callable[[], None]]:
    tenant = report_db.foundation.tenants["niletech"]
    group, user = tenant.groups["finance-team"].id, tenant.users["finance-reader"].id

    def revoke() -> None:
        owner_exec(
            report_db,
            "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u",
            {"g": group, "u": user},
        )

    yield revoke
    owner_exec(
        report_db,
        "INSERT INTO group_memberships (tenant_id, group_id, user_id) VALUES (:t, :g, :u)"
        " ON CONFLICT DO NOTHING",
        {"t": tenant.id, "g": group, "u": user},
    )


def test_reopening_an_old_answer_rechecks_current_access(
    client: TestClient,
    token: Callable[[str], str],
    revoke_finance_team: Callable[[], None],
) -> None:
    reply = chat(client, token(FINANCE), SALES_Q)
    assert reply.done["outcome"] == "published"
    conversation, run_id = reply.headers["x-conversation-id"], reply.headers["x-run-id"]
    revoke_finance_team()
    fresh = token(FINANCE)
    detail = client.get(f"/api/v1/conversations/{conversation}", headers=auth(fresh)).json()
    assert detail["turns"][0]["run_id"] == run_id  # the history itself is still the owner's
    again = replay(client, fresh, run_id)
    assert [f.data["code"] for f in again.of("error")] == ["access_revoked"]
    assert not again.of("token") and not again.of("citation")
    evidence_ids = {f.data.get("evidence_id") for f in reply.of("citation")} - {None}
    for evidence_id in evidence_ids:
        r = client.get(f"/api/v1/evidence/{evidence_id}", headers=auth(fresh))
        assert r.status_code == 404
