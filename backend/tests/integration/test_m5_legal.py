"""M5 legal pack on real PostgreSQL and Qdrant, through the real API (inline dispatch).

Expected values come from ``fixtures/legal/legal.toml``, computed by hand from the
contract texts, not from the extractor under test. The legal pack uses no model, so
these are complete results, not fake-inference stand-ins (the run is still created in
fake model mode and labelled so).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app.legal import workflow as legal_workflow
from tests.integration.conftest import owner_exec, tenant_packs
from tests.integration.document_support import auth
from tests.integration.m5_support import LEGAL_MANIFEST, M5, m5
from tests.integration.report_support import Stream, ask

__all__ = ["m5"]

FINANCE = "niletech/finance-reader"
BOARD = "niletech/board-member"
SALES = "niletech/sales-reader"
EXPECTED = {c["key"]: c["expected"] for c in LEGAL_MANIFEST["contracts"]}
TITLES = {c["key"]: c["title"] for c in LEGAL_MANIFEST["contracts"]}
MARKER = "NILETECH-LEGAL-RESTRICTED-5150"


def show(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B')} {d.year}"


@pytest.fixture(scope="module")
def contracts(m5: M5) -> Iterator[dict[str, str]]:
    ids = {c["key"]: m5.upload_contract(c["key"]) for c in LEGAL_MANIFEST["contracts"]}
    yield ids


def legal(m5: M5, ref: str, question: str) -> Stream:
    return ask(m5.client, m5.token(ref), question, mode="legal")


def open_citation(m5: M5, ref: str, stream: Stream, cid: str) -> Any:
    return m5.client.get(
        f"/api/v1/runs/{stream.run_id}/citations/{cid}", headers=auth(m5.token(ref))
    )


def sentences(stream: Stream) -> list[dict[str, Any]]:
    return [f.data for f in stream.of("token")]


def citations(stream: Stream) -> dict[str, dict[str, Any]]:
    return {f.data["citation_id"]: f.data for f in stream.of("citation")}


def assert_quotes_reopen(m5: M5, ref: str, stream: Stream) -> None:
    for cid, citation in citations(stream).items():
        opened = open_citation(m5, ref, stream, cid)
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["integrity"] == "verified"
        assert body["quote"] == citation["quote"]
        assert (body["char_start"], body["char_end"]) == (
            citation["char_start"],
            citation["char_end"],
        )


def test_expiry_answers_cite_the_exact_passages(m5: M5, contracts: dict[str, str]) -> None:
    stream = legal(m5, FINANCE, "When do our contracts expire?")
    assert stream.done["outcome"] == "published", stream.raw[-800:]
    assert stream.done["verification"]["label"] == "legal_facts_extracted_and_rechecked"
    assert stream.done["verification"]["model_used"] is False
    cited = citations(stream)
    by_title: dict[str, list[dict[str, Any]]] = {}
    for sentence in sentences(stream):
        title = sentence["text"].split(":", 1)[0]
        by_title.setdefault(title, []).append(sentence)
    # Red Sea: explicit expiry date, quoted verbatim.
    red = EXPECTED["red-sea-logistics"]
    (red_sentence,) = by_title[TITLES["red-sea-logistics"]]
    assert show(red["expiry"]) in red_sentence["text"]
    (cid,) = red_sentence["citations"]
    assert show(red["expiry"]) in cited[cid]["quote"]
    assert cited[cid]["document_id"] == contracts["red-sea-logistics"]
    # Aswan: 24 months from 1 April 2025 ends 31 March 2027 (two cited passages), and
    # the renewal notice deadline is 90 days earlier.
    aswan = EXPECTED["aswan-supply"]
    first, renewal = by_title[TITLES["aswan-supply"]]
    assert show(aswan["expiry"]) in first["text"] and "24 months" in first["text"]
    quotes = [cited[c]["quote"] for c in first["citations"]]
    assert any("1 April 2025" in q for q in quotes)
    assert any("twenty-four (24) months" in q for q in quotes)
    assert show(aswan["notice_deadline"]) in renewal["text"]
    assert any("ninety (90) days" in cited[c]["quote"] for c in renewal["citations"])
    # The restricted contract contributes nothing: no title, date, marker or citation.
    assert TITLES["nilecloud-licence"] not in stream.raw
    assert MARKER not in stream.raw and "14 January 2029" not in stream.raw
    assert contracts["nilecloud-licence"] not in {c["document_id"] for c in cited.values()}
    assert_quotes_reopen(m5, FINANCE, stream)


def test_clause_comparison_cites_each_contract(m5: M5, contracts: dict[str, str]) -> None:
    stream = legal(
        m5, FINANCE, "Compare the termination clauses of the Aswan and Red Sea agreements."
    )
    assert stream.done["outcome"] == "published", stream.raw[-800:]
    cited = citations(stream)
    texts = [s["text"] for s in sentences(stream)]
    aswan_days = EXPECTED["aswan-supply"]["termination_notice_days"]
    red_days = EXPECTED["red-sea-logistics"]["termination_notice_days"]
    aswan_line = next(t for t in texts if t.startswith(TITLES["aswan-supply"]))
    red_line = next(t for t in texts if t.startswith(TITLES["red-sea-logistics"]))
    assert f"{aswan_days} days" in aswan_line and f"{red_days} days" in red_line
    summary = sentences(stream)[-1]
    assert summary["text"].startswith("Longest termination period")
    assert len(summary["citations"]) == 2  # cites both compared passages
    for cid in summary["citations"]:
        assert "days" in cited[cid]["quote"] and "notice" in cited[cid]["quote"]
    assert {c["document_id"] for c in cited.values()} == {
        contracts["aswan-supply"],
        contracts["red-sea-logistics"],
    }
    assert_quotes_reopen(m5, FINANCE, stream)


def test_an_unreadable_contract_cannot_contribute_to_a_comparison(
    m5: M5, contracts: dict[str, str]
) -> None:
    question = "Compare termination notice in the Aswan, Red Sea and NileCloud agreements."
    finance = legal(m5, FINANCE, question)
    assert finance.done["outcome"] == "published"
    ids = {c["document_id"] for c in citations(finance).values()}
    assert ids == {contracts["aswan-supply"], contracts["red-sea-logistics"]}
    assert "NileCloud" not in finance.raw and MARKER not in finance.raw
    assert "90 days" not in finance.raw  # NileCloud's notice period
    # The board member reads NileCloud and Red Sea, not Aswan (finance-team only).
    board = legal(m5, BOARD, question)
    assert board.done["outcome"] == "published"
    ids = {c["document_id"] for c in citations(board).values()}
    assert ids == {contracts["nilecloud-licence"], contracts["red-sea-logistics"]}
    assert TITLES["aswan-supply"] not in board.raw and "60 days" not in board.raw


def normalized(stream: Stream) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in stream.frames:
        payload = {k: v for k, v in frame.data.items() if k not in ("run_id",)}
        out.append((frame.event, payload))
    return out


def test_only_an_unreadable_contract_looks_like_a_missing_one(
    m5: M5, contracts: dict[str, str]
) -> None:
    unreadable = legal(m5, FINANCE, "When does the NileCloud licence expire?")
    missing = legal(m5, FINANCE, "When does the Zephyrine licence expire?")
    assert unreadable.done["abstention"]["code"] == "no_sources"
    assert normalized(unreadable) == normalized(missing)
    assert "NileCloud" not in "".join(str(p) for _, p in normalized(unreadable))


def test_access_revoked_before_publication_publishes_nothing(
    m5: M5, contracts: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    red = contracts["red-sea-logistics"]
    original = legal_workflow.LegalAnswerWorkflow.build_answer

    def build_then_revoke(self: Any, state: Any) -> Any:
        # Runs inside publication, after verification and before the recheck lock.
        result = original(self, state)
        owner_exec(
            m5.db,
            "UPDATE resource_grants SET effect = 'deny' WHERE resource_id = :r",
            {"r": red},
        )
        return result

    monkeypatch.setattr(legal_workflow.LegalAnswerWorkflow, "build_answer", build_then_revoke)
    try:
        stream = legal(m5, SALES, "When does the Red Sea agreement expire?")
    finally:
        owner_exec(
            m5.db,
            "UPDATE resource_grants SET effect = 'allow' WHERE resource_id = :r",
            {"r": red},
        )
    assert stream.done["outcome"] == "failed"
    assert {f.data["code"] for f in stream.of("error")} == {"access_revoked"}
    assert not stream.of("token") and not stream.of("citation")
    assert "30 June 2026" not in stream.raw


def test_year_filter_and_auto_routing(m5: M5, contracts: dict[str, str]) -> None:
    stream = ask(m5.client, m5.token(FINANCE), "Which contracts expire in 2027?")
    kind = owner_exec(
        m5.db, "SELECT kind FROM report_runs WHERE id = :r", {"r": stream.run_id}
    ).scalar_one()
    assert kind == "legal"  # not the sales report, despite "in 2027"
    assert stream.done["outcome"] == "published"
    titles = {s["text"].split(":", 1)[0] for s in sentences(stream)}
    assert titles == {TITLES["aswan-supply"]}


def test_legal_pack_is_backend_enforced(m5: M5, contracts: dict[str, str]) -> None:
    with tenant_packs(m5.db, "niletech", ["documents", "sales_reports"]):
        response = m5.client.post(
            "/api/v1/chat",
            json={"question": "When do our contracts expire?", "mode": "legal"},
            headers=auth(m5.token(FINANCE)),
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "capability_disabled"


def test_documents_answers_do_not_search_repository_code(m5: M5, contracts: dict[str, str]) -> None:
    """Legal and documents packs exclude repository sources (code pack only)."""

    stream = legal(m5, FINANCE, "Compare the payment terms of the Aswan and Red Sea agreements.")
    assert stream.done["outcome"] == "published"
    texts = " ".join(s["text"] for s in sentences(stream))
    assert "45 days" in texts and "30 days" in texts
