"""Chat orchestrator (M8): picks the handler for a question typed in the chat.

The employee no longer chooses a source. For each question this module picks exactly one
of the existing handlers, or asks one short clarifying question:

* ``general``: greetings, small talk and ordinary questions that do not ask about the
  company (the model alone; no documents, data or actions);
* ``documents``: company policies, memos, handbooks, people and processes (authorized
  document answers with citations);
* ``report``: sales, revenue and order figures (the verified SQL report);
* ``legal``: contract clauses and expiry (exact contract passages);
* ``code``: the company's code repositories;
* ``clarify``: the question is too vague to route (a short reply asks for detail; no run).

The decision is made on the server by fixed rules, never by the client and never by model
output, and only among the handlers the tenant has enabled. Each handler then applies its
own permission checks, verification and citations unchanged, so routing can never widen
what an employee may see: a question routed to documents the employee cannot open still
ends with the documents handler's no-sources refusal.

Briefings and explicit API modes keep using ``app.api.reports.choose_kind``; General is
never a briefing or email source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.reporting.router import _TOPIC, _YEAR_ONLY, extract_periods
from app.tenancy.config import RUN_KIND_PACK

Handler = Literal["general", "documents", "report", "legal", "code"]
Route = Literal["general", "documents", "report", "legal", "code", "clarify"]

HANDLER_LABELS: dict[str, str] = {
    "general": "General",
    "documents": "Documents",
    "report": "Sales report (SQL)",
    "legal": "Contracts",
    "code": "Code repositories",
    "clarify": "Clarifying question",
}

_GREETING = re.compile(
    r"^(hi|hello|hey|hiya|yo|salam|ahlan|good\s+(morning|afternoon|evening|day)|"
    r"thanks|thank\s+you|thx|cheers|bye|goodbye|see\s+you|"
    r"how\s+are\s+you|who\s+are\s+you|what\s+can\s+you\s+do|help)\b",
    re.I,
)
_LEGAL = re.compile(
    r"\b(contracts?|agreements?|clauses?|licen[cs]es?|termination\s+terms?)\b", re.I
)
# Company code: a repository, a path, a function, an endpoint, or "our/the ... code".
# General programming questions ("how do I write a Python loop?") stay General.
_CODE = re.compile(
    r"\b(codebase|source\s+code|repositor(?:y|ies)|repos?|endpoints?)\b|"
    r"\b(our|the|this|in\s+the)\s+code\b|"
    r"\bfunction\s+`?\w+|\b\w+\(\)|`[^`]+`|"
    r"\b[\w/-]+\.(?:py|ts|tsx|js|jsx|go|java|rs|sql)\b|"
    r"\bwhere\s+is\s+[\w\s]{1,40}\bimplemented\b",
    re.I,
)
# Company documents: policies, people, processes and the organisation's own records.
_DOCUMENTS = re.compile(
    r"\b(polic(?:y|ies)|handbook|guidelines?|procedures?|leave|vacation|holidays?|"
    r"annual\s+leave|sick|maternity|paternity|parental|allowance|carry\s*(?:over|forward)|"
    r"roll\s*over|benefits?|payroll|salar(?:y|ies)|bonus(?:es)?|overtime|working\s+hours|"
    r"remote\s+work|expenses?|reimburse\w*|travel|per\s+diem|onboarding|hr|"
    r"human\s+resources|employees?|staff|colleagues?|manager|department|team\s+lead|"
    r"memos?|minutes|board|strategy|commentary|catalogue|catalog|customers?|clients?|"
    r"suppliers?|deliver(?:y|ies)|security\s+handbook|it\s+security|passwords?|"
    r"company|organi[sz]ation|internal|niletech|our)\b",
    re.I,
)
# Explanations ("why did collections lag revenue?") are answered from documents such as
# commentary and memos; the SQL report returns figures, not reasons.
_WHY = re.compile(r"^\s*(why|what\s+caused|what\s+explains|explain\s+why)\b", re.I)
# Figures the sales report can return.
_FIGURES = re.compile(
    r"\b(how\s+much|how\s+many|total|what\s+were|what\s+was|figures?|numbers|amount)\b", re.I
)
# Vague requests that name no subject the rules can route.
_VAGUE = re.compile(
    r"\b(it|that|this|those|these|them|the\s+(report|numbers|figures|data|results|file|"
    r"document|one)|numbers|figures|data|results|status|update|details|more|info)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class ChatRoute:
    route: Route
    reason: str

    @property
    def label(self) -> str:
        return HANDLER_LABELS[self.route]


def _words(text: str) -> list[str]:
    return re.findall(r"[\w'\u2019-]+", text)


def _company_handler(text: str) -> tuple[Handler, str] | None:
    """The company handler the question names, or None when it names none."""

    if _LEGAL.search(text):
        return "legal", "contract_terms"
    if _CODE.search(text):
        return "code", "repository_terms"
    sales = bool(_TOPIC.search(text))
    documents = bool(_DOCUMENTS.search(text))
    if sales and _WHY.search(text):
        return "documents", "explanation_of_figures"
    if sales:
        return "report", "sales_terms"
    if documents:
        return "documents", "company_terms"
    # A named quarter or month ("the numbers for Q1 2026") asks for business figures; a
    # bare year only with figure words ("what was the total in 2026?").
    if extract_periods(text):
        return "report", "reporting_period"
    if _YEAR_ONLY.search(text) and _FIGURES.search(text):
        return "report", "figures_for_year"
    return None


def route_chat(question: str, enabled_packs: frozenset[str] | None) -> ChatRoute:
    """Choose the handler for one chat question among the tenant's enabled packs."""

    def on(handler: str) -> bool:
        return enabled_packs is None or RUN_KIND_PACK[handler] in enabled_packs

    text = " ".join(question.split())
    words = _words(text)
    if not words:
        return ChatRoute("clarify", "empty")

    if _GREETING.match(text) and len(words) <= 6:
        return (
            ChatRoute("general", "greeting")
            if on("general")
            else ChatRoute("clarify", "greeting_without_general")
        )

    company = _company_handler(text)
    if company is not None:
        handler, reason = company
        if on(handler):
            return ChatRoute(handler, reason)
        # The named handler is not enabled for this tenant: fall back to the nearest
        # company handler that is, as the explicit router does; never to General.
        for other in ("documents", "report"):
            if on(other):
                return ChatRoute(other, f"{reason}_fallback")
        return ChatRoute("clarify", f"{reason}_disabled")

    if len(words) <= 4 and _VAGUE.search(text):
        return ChatRoute("clarify", "vague")
    if len(words) <= 2 and not text.rstrip().endswith("?"):
        # One or two words with no subject the rules know ("budget", "Q3"): ask.
        return ChatRoute("clarify", "too_short")
    if on("general"):
        return ChatRoute("general", "no_company_terms")
    if on("documents"):
        return ChatRoute("documents", "general_disabled")
    return ChatRoute("clarify", "no_enabled_handler")


def clarifying_question(enabled_packs: frozenset[str] | None) -> str:
    """One short question naming only the handlers this tenant can use."""

    def on(handler: str) -> bool:
        return enabled_packs is None or RUN_KIND_PACK[handler] in enabled_packs

    options = []
    if on("documents"):
        options.append("a company policy or document")
    if on("report"):
        options.append("sales figures for a period")
    if on("legal"):
        options.append("a contract")
    if on("code"):
        options.append("company code")
    if on("general"):
        options.append("a general question")
    if not options:
        return "Could you say a little more about what you need?"
    if len(options) == 1:
        return f"Could you say a little more? I can help with {options[0]}."
    return f"Could you say a little more? Is this about {', '.join(options[:-1])} or {options[-1]}?"
