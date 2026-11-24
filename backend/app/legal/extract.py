"""Deterministic contract analysis for the legal pack (M5, ADR 0501).

Everything here is pure and model-free: given a version's canonical text and element
spans, it finds clauses (by heading) and dated facts, each tied to the exact
code-point span of the sentence it came from. The workflow cites those spans, and the
verifier re-runs these extractors on the cited quotes alone, so a published value is
always reproducible from the text the reader can open.

Conventions (stated in every answer that uses them):

* A term of N months starting on date D **ends on the day before D + N months** (the
  last day of the term, inclusive): 1 April 2025 + 24 months -> 31 March 2027.
* An explicit expiry date in the text wins over a computed one.
* A renewal notice deadline is the expiry date minus the notice period in days.

Only English contract language is recognised (the project's initial acceptance
language). Unrecognised wording yields no fact rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
MONTH_NAMES = {v: k.capitalize() for k, v in MONTHS.items()}
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "eighteen": 18,
    "twenty-four": 24,
    "thirty": 30,
    "thirty-six": 36,
    "forty-five": 45,
    "sixty": 60,
    "ninety": 90,
}
_MONTH_RE = "|".join(MONTHS)
DATE_RE = re.compile(
    rf"\b(?:(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MONTH_RE})\s+(?P<y1>\d{{4}})"
    rf"|(?P<m2>{_MONTH_RE})\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})"
    r"|(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2}))\b",
    re.I,
)
_QTY = r"(?:(?P<w>[a-z]+(?:-[a-z]+)?)\s*)?(?:\(\s*(?P<n>\d{1,3})\s*\)|(?P<n2>\d{1,3}))?"
TERM_RE = re.compile(
    r"\b(?:initial\s+)?term(?:\s+of\s+(?:this|the)\s+(?:agreement|contract|licen[cs]e))?\s+"
    r"(?:is\s+|of\s+|shall\s+be\s+|will\s+be\s+)(?:a\s+period\s+of\s+)?"
    rf"{_QTY}\s*[- ]?(?P<unit>months?|years?)\b",
    re.I,
)
RENEWAL_RE = re.compile(
    r"\b(?:automatically\s+)?renew\w*\s+for\s+(?:successive|additional|further|consecutive)?\s*"
    rf"(?:periods?\s+of\s+)?{_QTY}\s*[- ]?(?P<unit>months?|years?)\b",
    re.I,
)
NOTICE_DAYS_RE = re.compile(
    r"\b(?:(?P<w>[a-z]+(?:-[a-z]+)?)\s*)?(?:\(\s*(?P<n>\d{1,3})\s*\)|(?P<n2>\d{1,3}))\s*"
    r"(?:calendar\s+|business\s+)?days?['\u2019]?\s+(?:prior\s+|advance\s+)?(?:written\s+)?notice\b",
    re.I,
)
PAYMENT_DAYS_RE = re.compile(r"\bwithin\s+(?P<n>\d{1,3})\s+(?:calendar\s+)?days\b", re.I)
LIABILITY_CAP_RE = re.compile(
    r"\b(?:shall\s+not\s+exceed|capped\s+at|limited\s+to|will\s+not\s+exceed|maximum\s+of)\s+"
    r"(?P<cap>[^.;]{3,160})",
    re.I,
)
GOVERNING_LAW_RE = re.compile(r"\blaws?\s+of\s+(?P<law>(?:the\s+)?[A-Z][\w .'-]{1,60}?)(?=[.,;]|$)")
SURVIVAL_RE = re.compile(r"\b(?P<n>\d{1,2})\s*\(?\w*\)?\s*years?\s+(?:after|following)\b", re.I)
EFFECTIVE_KW = re.compile(
    r"\b(?:effective\s+(?:date|as\s+of|from|on)|commenc\w*|start\s+date|begins?\s+on|"
    r"enters?\s+into\s+force)\b",
    re.I,
)
EXPIRY_KW = re.compile(
    r"\b(?:expir\w*|end\s+date|ends?\s+on|terminates?\s+(?:automatically\s+)?on|"
    r"valid\s+(?:until|through)|remains?\s+in\s+force\s+until|until)\b",
    re.I,
)
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Clause kinds: heading keywords -> kind. Questions use the same words plus synonyms.
CLAUSE_KINDS: dict[str, tuple[str, ...]] = {
    "termination": (r"terminat\w*", r"notice\s+period", r"cancel\w*"),
    "liability": (r"liabilit\w*", r"liability\s+cap", r"\bcap\b"),
    "payment": (r"payment\w*", r"\bfees?\b", r"invoic\w*"),
    "confidentiality": (r"confidential\w*", r"non-disclosure"),
    "governing_law": (r"governing\s+law", r"jurisdiction", r"\blaw\b"),
    "term": (r"\bterm\b", r"\bduration\b", r"renew\w*"),
    "indemnity": (r"indemn\w*",),
    "warranty": (r"warrant\w*",),
    "data_protection": (r"data\s+protection", r"privacy", r"personal\s+data"),
    "service_levels": (r"service\s+levels?", r"\bsla\b", r"uptime"),
}
CLAUSE_LABELS = {
    "termination": "Termination",
    "liability": "Limitation of liability",
    "payment": "Payment",
    "confidentiality": "Confidentiality",
    "governing_law": "Governing law",
    "term": "Term and renewal",
    "indemnity": "Indemnity",
    "warranty": "Warranties",
    "data_protection": "Data protection",
    "service_levels": "Service levels",
}
CONTRACT_SIGNAL = re.compile(
    r"\b(?:this\s+(?:agreement|contract)|the\s+parties|by\s+and\s+between|between\b.{0,200}"
    r"\band\b)",
    re.I | re.S,
)
_SENTENCE_END = re.compile(r"(?<=[.;:])\s+|\n+")


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # code points into the canonical text, inclusive
    end: int  # exclusive
    text: str


@dataclass(frozen=True, slots=True)
class Fact:
    name: str  # effective | expiry | term_months | renewal_months | renewal_notice_days
    value: Any
    span: Span


@dataclass(frozen=True, slots=True)
class Clause:
    kind: str
    heading: str
    span: Span  # the clause body (paragraphs under the heading)


@dataclass(frozen=True, slots=True)
class Attribute:
    """A comparable value of a clause (notice days, payment days, cap text, law)."""

    name: str
    value: Any
    display: str
    span: Span


@dataclass
class ContractFacts:
    is_contract: bool
    facts: dict[str, Fact] = field(default_factory=dict)
    clauses: list[Clause] = field(default_factory=list)

    def clause(self, kind: str) -> Clause | None:
        return next((c for c in self.clauses if c.kind == kind), None)


# -- small parsers -------------------------------------------------------------------------


def parse_date(match: re.Match[str]) -> date | None:
    g = match.groupdict()
    try:
        if g["y1"]:
            return date(int(g["y1"]), MONTHS[g["m1"].lower()], int(g["d1"]))
        if g["y2"]:
            return date(int(g["y2"]), MONTHS[g["m2"].lower()], int(g["d2"]))
        return date(int(g["y3"]), int(g["m3"]), int(g["d3"]))
    except ValueError:
        return None


def show_date(value: date) -> str:
    return f"{value.day} {MONTH_NAMES[value.month]} {value.year}"


def _quantity(match: re.Match[str]) -> int | None:
    g = match.groupdict()
    digits = g.get("n") or g.get("n2")
    if digits:
        return int(digits)
    word = (g.get("w") or "").lower()
    return NUMBER_WORDS.get(word)


def _months(match: re.Match[str]) -> int | None:
    quantity = _quantity(match)
    if quantity is None:
        return None
    return quantity * 12 if match.group("unit").lower().startswith("year") else quantity


def add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    day = start.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def term_end(start: date, months: int) -> date:
    """Last day of a term of ``months`` starting on ``start`` (inclusive convention)."""

    return add_months(start, months) - timedelta(days=1)


def sentences(text: str, start: int, end: int) -> list[Span]:
    """Sentence spans inside ``[start, end)`` of ``text`` (exact code points)."""

    out: list[Span] = []
    cursor = start
    body = text[start:end]
    for match in _SENTENCE_END.finditer(body):
        stop = start + match.start()
        if stop > cursor and text[cursor:stop].strip():
            out.append(_trim(text, cursor, stop))
        cursor = start + match.end()
    if cursor < end and text[cursor:end].strip():
        out.append(_trim(text, cursor, end))
    return out


def _trim(text: str, start: int, end: int) -> Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    # Leading list markers / clause numbers stay out of the quote.
    marker = re.match(r"(?:[-*+]\s+|\d+(?:\.\d+)*[.)]?\s+)", text[start:end])
    if marker and marker.end() < end - start:
        start += marker.end()
    return Span(start, end, text[start:end])


# -- clauses -------------------------------------------------------------------------------


def clause_kind(heading: str) -> str | None:
    lowered = heading.lower()
    if re.search(r"terminat", lowered):
        return "termination"  # before "term"
    for kind, patterns in CLAUSE_KINDS.items():
        if any(re.search(p, lowered) for p in patterns):
            return kind
    return None


def question_clause_kinds(question: str) -> list[str]:
    lowered = question.lower()
    found = []
    for kind, patterns in CLAUSE_KINDS.items():
        if any(re.search(p, lowered) for p in patterns):
            found.append(kind)
    if "termination" in found and "term" in found and not re.search(r"\bterm\b", lowered):
        found.remove("term")
    return found


def find_clauses(text: str, elements: list[dict[str, Any]] | None) -> list[Clause]:
    """Clauses = the paragraphs under each recognised heading, from stored element spans
    (Markdown/Docling headings). Without headings, numbered heading lines are used."""

    clauses: list[Clause] = []
    if elements:
        heads = [e for e in elements if e.get("type") == "heading"]
        for head in heads:
            title = text[int(head["start"]) : int(head["end"])].strip()
            kind = clause_kind(re.sub(r"^\d+(?:\.\d+)*[.)]?\s*", "", title))
            if kind is None:
                continue
            body = [
                e
                for e in elements
                if e.get("type") != "heading"
                and int(e["start"]) > int(head["end"])
                and (e.get("heading_path") or [None])[-1] == title
            ]
            if body:
                start, end = int(body[0]["start"]), int(body[-1]["end"])
                clauses.append(Clause(kind, title, Span(start, end, text[start:end])))
        if clauses:
            return _first_per_kind(clauses)
    heading_lines = list(re.finditer(r"(?m)^(\d+(?:\.\d+)*[.)]?\s+[A-Z][^\n]{2,80})$", text))
    for index, match in enumerate(heading_lines):
        kind = clause_kind(re.sub(r"^\d+(?:\.\d+)*[.)]?\s*", "", match.group(1)))
        if kind is None:
            continue
        start = match.end()
        end = heading_lines[index + 1].start() if index + 1 < len(heading_lines) else len(text)
        span = _trim(text, start, end)
        if span.text:
            clauses.append(Clause(kind, match.group(1).strip(), span))
    return _first_per_kind(clauses)


def _first_per_kind(clauses: list[Clause]) -> list[Clause]:
    seen: set[str] = set()
    out = []
    for clause in clauses:
        if clause.kind not in seen:
            seen.add(clause.kind)
            out.append(clause)
    return out


# -- facts ---------------------------------------------------------------------------------


def _date_after(span: Span, keyword: re.Pattern[str]) -> date | None:
    kw = keyword.search(span.text)
    if kw is None:
        return None
    for match in DATE_RE.finditer(span.text, kw.end()):
        value = parse_date(match)
        if value is not None:
            return value
    return None


def sentence_facts(span: Span) -> dict[str, Any]:
    """Facts stated in one sentence. Used for extraction and, on a cited quote alone,
    for verification (the published value must be re-derivable from the quote)."""

    found: dict[str, Any] = {}
    effective = _date_after(span, EFFECTIVE_KW)
    if effective is not None:
        found["effective"] = effective
    expiry = _date_after(span, EXPIRY_KW)
    if expiry is not None and expiry != effective:
        found["expiry"] = expiry
    if (term := TERM_RE.search(span.text)) and (months := _months(term)):
        found["term_months"] = months
    renewal = RENEWAL_RE.search(span.text)
    if renewal and (months := _months(renewal)):
        found["renewal_months"] = months
    notice = NOTICE_DAYS_RE.search(span.text) if re.search(r"renew", span.text, re.I) else None
    if notice is not None and (days := _quantity(notice)) is not None:
        found["renewal_notice_days"] = days
    return found


def analyse(text: str, elements: list[dict[str, Any]] | None) -> ContractFacts:
    clauses = find_clauses(text, elements)
    is_contract = bool(CONTRACT_SIGNAL.search(text[:4000])) and len(clauses) >= 2
    facts: dict[str, Fact] = {}
    if is_contract:
        for span in sentences(text, 0, len(text)):
            for name, value in sentence_facts(span).items():
                facts.setdefault(name, Fact(name, value, span))
    return ContractFacts(is_contract, facts, clauses)


def expiry_of(contract: ContractFacts) -> dict[str, Any] | None:
    """The contract's expiry: explicit, or computed from effective date + term."""

    explicit = contract.facts.get("expiry")
    if explicit is not None:
        return {"date": explicit.value, "derived": False, "facts": [explicit]}
    effective, term = contract.facts.get("effective"), contract.facts.get("term_months")
    if effective is not None and term is not None:
        return {
            "date": term_end(effective.value, int(term.value)),
            "derived": True,
            "facts": [effective, term],
        }
    return None


# -- clause attributes -----------------------------------------------------------------------


def clause_attribute(text: str, clause: Clause) -> Attribute | None:
    """One comparable value per clause kind, with the exact sentence it came from."""

    for span in sentences(text, clause.span.start, clause.span.end):
        value = attribute_in(clause.kind, span)
        if value is not None:
            return value
    return None


def attribute_in(kind: str, span: Span) -> Attribute | None:
    s = span.text
    if kind == "termination" and (m := NOTICE_DAYS_RE.search(s)):
        days = _quantity(m)
        if days is not None:
            return Attribute("notice_days", days, f"{days} days' notice", span)
    if kind == "payment" and (m := PAYMENT_DAYS_RE.search(s)):
        days = int(m.group("n"))
        return Attribute("payment_days", days, f"payment due within {days} days", span)
    if kind == "liability" and (m := LIABILITY_CAP_RE.search(s)):
        cap = m.group("cap").strip().rstrip(",")
        return Attribute("liability_cap", cap, f"liability capped at {cap}", span)
    if kind == "governing_law" and (m := GOVERNING_LAW_RE.search(s)):
        law = m.group("law").strip()
        return Attribute("governing_law", law, f"governed by the laws of {law}", span)
    if kind == "confidentiality" and (m := SURVIVAL_RE.search(s)):
        years = int(m.group("n"))
        return Attribute("survival_years", years, f"confidentiality survives {years} years", span)
    return None


def numbers(value: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in NUMBER_RE.finditer(value)}
