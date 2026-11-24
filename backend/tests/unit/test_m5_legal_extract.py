"""Legal extraction against the hand-computed expectations in fixtures/legal/legal.toml
and against wording variants (no model involved)."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.documents.canonical import elements_to_json
from app.documents.parsers import parse
from app.legal import extract as lx
from app.legal.workflow import legal_intent, named_contracts

LEGAL = Path(__file__).resolve().parents[2] / "fixtures" / "legal"
MANIFEST = tomllib.loads((LEGAL / "legal.toml").read_text())


def analyse(file: str) -> tuple[str, lx.ContractFacts]:
    doc = parse(file, (LEGAL / "files" / file).read_bytes())
    return doc.text, lx.analyse(doc.text, elements_to_json(doc))


def test_fixture_hashes_match() -> None:
    import hashlib

    for line in (LEGAL / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((LEGAL / "files" / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("contract", MANIFEST["contracts"], ids=lambda c: c["key"])
def test_contract_facts_match_the_hand_computed_reference(contract: dict[str, Any]) -> None:
    text, facts = analyse(contract["file"])
    expected = contract["expected"]
    assert facts.is_contract
    assert facts.facts["effective"].value == date.fromisoformat(expected["effective"])
    expiry = lx.expiry_of(facts)
    assert expiry is not None
    assert expiry["date"] == date.fromisoformat(expected["expiry"])
    assert expiry["derived"] is expected["expiry_derived"]
    termination = facts.clause("termination")
    assert termination is not None
    attribute = lx.clause_attribute(text, termination)
    assert attribute is not None and attribute.value == expected["termination_notice_days"]
    assert text[attribute.span.start : attribute.span.end] == attribute.span.text
    if "payment_days" in expected:
        payment = lx.clause_attribute(text, facts.clause("payment"))  # type: ignore[arg-type]
        assert payment is not None and payment.value == expected["payment_days"]
    if "renewal_notice_days" in expected:
        notice = facts.facts["renewal_notice_days"]
        assert notice.value == expected["renewal_notice_days"]
        deadline = date.fromordinal(expiry["date"].toordinal() - notice.value)
        assert deadline == date.fromisoformat(expected["notice_deadline"])
    for fact in facts.facts.values():  # every fact is an exact span of the text
        assert text[fact.span.start : fact.span.end] == fact.span.text


@pytest.mark.parametrize(
    ("start", "months", "end"),
    [
        (date(2025, 4, 1), 24, date(2027, 3, 31)),
        (date(2026, 1, 15), 36, date(2029, 1, 14)),
        (date(2024, 1, 31), 1, date(2024, 2, 28)),  # Feb 29 2024 minus a day
        (date(2023, 1, 31), 1, date(2023, 2, 27)),
        (date(2024, 2, 29), 12, date(2025, 2, 27)),
    ],
)
def test_term_end_convention(start: date, months: int, end: date) -> None:
    assert lx.term_end(start, months) == end


@pytest.mark.parametrize(
    ("sentence", "found"),
    [
        ("This Agreement expires on March 31, 2027.", {"expiry": date(2027, 3, 31)}),
        ("The end date is 2026-12-31.", {"expiry": date(2026, 12, 31)}),
        ("It remains in force until 1st June 2028.", {"expiry": date(2028, 6, 1)}),
        ("The term of the contract shall be 18 months.", {"term_months": 18}),
        ("The initial term of this Agreement is two (2) years.", {"term_months": 24}),
        (
            "It renews for successive periods of one year unless 30 days' written notice is given.",
            {"renewal_months": 12, "renewal_notice_days": 30},
        ),
        ("Payment is due promptly.", {}),
        ("Notice must be given 31 February 2026.", {}),  # impossible date: no fact
    ],
)
def test_sentence_facts(sentence: str, found: dict[str, Any]) -> None:
    assert lx.sentence_facts(lx.Span(0, len(sentence), sentence)) == found


def test_clause_attributes() -> None:
    def attr(kind: str, s: str) -> object:
        got = lx.attribute_in(kind, lx.Span(0, len(s), s))
        return got.value if got else None

    assert attr("termination", "Either party may terminate on 45 days written notice.") == 45
    assert attr("payment", "Invoices are payable within 60 days.") == 60
    assert attr("liability", "Liability is capped at USD 10,000.") == "USD 10,000"
    assert attr("governing_law", "Governed by the laws of the State of New York.") == (
        "the State of New York"
    )
    assert attr("termination", "Termination requires mutual agreement.") is None


def test_intent_and_names() -> None:
    assert legal_intent("When does the Aswan agreement expire?")["intent"] == "expiry"
    assert legal_intent("Compare termination clauses")["clause_kinds"] == ["termination"]
    intent = legal_intent("Which contracts expire before 1 January 2027?")
    assert intent["before"] == date(2027, 1, 1)
    assert legal_intent("Contracts ending in 2027")["year"] == 2027
    titles = {"a": "Master Supply Agreement — Aswan Components", "b": "Logistics — Red Sea"}
    assert named_contracts("Compare the Aswan and Red Sea contracts", titles) == (["a", "b"], False)
    assert named_contracts("When does the Zephyr licence expire?", titles) == ([], True)
    assert named_contracts("Which contracts expire in 2027?", titles) == ([], False)


def test_non_contracts_are_ignored() -> None:
    doc = parse("policy.md", b"# Leave policy\n\n## Annual leave\n\nStaff get 21 days.\n")
    assert not lx.analyse(doc.text, elements_to_json(doc)).is_contract
