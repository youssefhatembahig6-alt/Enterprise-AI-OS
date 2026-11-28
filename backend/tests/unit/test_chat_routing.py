"""M8 chat orchestrator: one handler per question, decided on the server by rules."""

from __future__ import annotations

import pytest

from app.chat.routing import HANDLER_LABELS, clarifying_question, route_chat

EVERY = frozenset({"documents", "sales_reports", "legal", "code", "general", "actions"})


@pytest.mark.parametrize(
    "question",
    [
        "hello",
        "Hi!",
        "Good morning",
        "thanks",
        "How are you?",
        "What can you do?",
    ],
)
def test_greetings_get_a_natural_general_reply(question: str) -> None:
    assert route_chat(question, EVERY).route == "general"


@pytest.mark.parametrize(
    "question",
    [
        "Explain in plain words what a confusion matrix is.",
        "Give me three tips for writing a clear project status email.",
        "Suggest a simple agenda for a 30-minute weekly team retrospective.",
        "What are two common causes of slow SQL queries and how can I spot them?",
        "Write a haiku about rivers",
        "What is a balance sheet?",
        "How do I write a for loop in Python?",
    ],
)
def test_ordinary_questions_go_to_general(question: str) -> None:
    route = route_chat(question, EVERY)
    assert route.route == "general", route


@pytest.mark.parametrize(
    "question",
    [
        "If I don't use all my vacation this year, how many days can I roll over, and until when?",
        "How many unused leave days can I carry into next year, and when do they expire?",
        "What is the annual leave allowance for staff in Egypt after ten years of service?",
        "Who do I contact in HR about family leave?",
        "Are public holidays that fall during my approved leave deducted from my allowance?",
        "What is the method for expense claims?",
        "What is the notice period for leave?",
        "What does the IT security handbook say about passwords?",
        # Explanations of figures come from commentary and memos, not the SQL report.
        "Why did collections lag revenue in Q1 2026?",
    ],
)
def test_company_policy_and_records_questions_go_to_documents(question: str) -> None:
    route = route_chat(question, EVERY)
    assert route.route == "documents", route


@pytest.mark.parametrize(
    "question",
    [
        "What were fulfilled sales in Q1 2026?",
        "What was total revenue in February 2026?",
        "How much did we sell in Q1 2026?",
        "What were the total figures for Q1 2026?",
        "Give me the numbers for Q1 2026",
        "What was the total in 2026?",
    ],
)
def test_sales_figures_go_to_the_verified_sql_report(question: str) -> None:
    assert route_chat(question, EVERY).route == "report"


def test_contract_and_code_questions_go_to_their_handlers() -> None:
    assert route_chat("Which contracts expire in 2027?", EVERY).route == "legal"
    assert route_chat("Compare the termination clauses", EVERY).route == "legal"
    assert route_chat("Where is the reserve function implemented?", EVERY).route == "code"
    assert route_chat("What does app/main.py configure?", EVERY).route == "code"
    assert route_chat("Which endpoints does the repository expose?", EVERY).route == "code"


@pytest.mark.parametrize("question", ["the numbers?", "Show me the report", "status", "budget"])
def test_vague_requests_get_a_clarifying_question(question: str) -> None:
    route = route_chat(question, EVERY)
    assert route.route == "clarify", route
    assert route.label == "Clarifying question"


def test_only_enabled_handlers_are_routed_and_never_general_for_company_questions() -> None:
    base = frozenset({"documents", "sales_reports"})
    # The named handler is disabled: the nearest company handler, never General.
    assert route_chat("Which contracts expire in 2027?", base).route == "documents"
    assert route_chat("What were fulfilled sales in Q1 2026?", frozenset({"documents"})).route == (
        "documents"
    )
    # Without the general pack, greetings ask for detail and ordinary questions go to
    # documents (whose own no-sources refusal applies).
    assert route_chat("hello", base).route == "clarify"
    assert route_chat("Explain what a confusion matrix is.", base).route == "documents"
    assert route_chat("What is the leave policy?", frozenset()).route == "clarify"


def test_the_clarifying_question_names_only_enabled_handlers() -> None:
    every = clarifying_question(EVERY)
    assert "sales figures" in every and "contract" in every and "general question" in every
    documents_only = clarifying_question(frozenset({"documents"}))
    assert "policy" in documents_only
    assert "sales" not in documents_only and "contract" not in documents_only
    assert clarifying_question(frozenset())


def test_every_route_has_a_label() -> None:
    assert set(HANDLER_LABELS) == {"general", "documents", "report", "legal", "code", "clarify"}
    assert HANDLER_LABELS["report"] == "Sales report (SQL)"
