"""Authorized document answers on real PostgreSQL and real Qdrant (T1-T5, T7-T10).

Model doubles are the explicit FakeModelClient (feature-hashing embeddings, quoting
answerer); runs are labelled fake. This is security and integration evidence, not
retrieval-quality evidence (see the retrieval measurement in the PR).
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import RetrievalMode
from app.main import create_app
from app.reporting.model_client import CheckResult, FakeModelClient, GatewayUnavailable
from tests.integration.conftest import DbHarness, document_settings, login, owner_exec
from tests.integration.document_support import NILETECH_MARKERS, auth, seed_corpus
from tests.integration.report_support import Stream, ask

FINANCE_Q = "How many invoices were overdue at the reference date?"
FINANCE_QUOTE_PART = "370 open invoices were already past their due date"


@pytest.fixture(scope="module")
def corpus(
    documents_db: DbHarness,
    stub_http_url: str,
    qdrant_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[TestClient, dict[str, str], Any]]:
    prefix = "ta" + uuid.uuid4().hex[:8]
    settings = document_settings(
        documents_db, stub_http_url, qdrant_url, str(tmp_path_factory.mktemp("objects")), prefix
    )
    with TestClient(create_app(settings)) as client:
        ids = seed_corpus(client, documents_db)
        yield client, ids, settings
        client.app.state.documents._qdrant.delete_collection(  # type: ignore[attr-defined]
            client.app.state.report_runner.documents_workflow.collection()  # type: ignore[attr-defined]
        )


def workflow(client: TestClient) -> Any:
    return client.app.state.report_runner.documents_workflow  # type: ignore[attr-defined]


@pytest.fixture
def model(
    corpus: tuple[TestClient, dict[str, str], Any],
) -> Iterator[Callable[[FakeModelClient], None]]:
    client = corpus[0]
    original = workflow(client).deps.model

    def use(fake: FakeModelClient) -> None:
        workflow(client).deps.model = fake

    yield use
    workflow(client).deps.model = original


def ask_docs(client: TestClient, db: DbHarness, ref: str, question: str) -> Stream:
    return ask(client, login(client, db, ref), question, mode="documents")


def assert_no_markers(text_: str, allowed: tuple[str, ...] = ()) -> None:
    for marker in [*NILETECH_MARKERS, "ISOLATION-FINANCE-SECRET-5005"]:
        if marker not in allowed:
            assert marker not in text_, f"leaked {marker}"


def open_citation(client: TestClient, token: str, run_id: str, cid: str) -> Any:
    return client.get(f"/api/v1/runs/{run_id}/citations/{cid}", headers=auth(token))


# -- T1 and citations ---------------------------------------------------------------------


def test_t1_authorized_answer_with_exact_citation(corpus: Any, documents_db: DbHarness) -> None:
    client, ids, _ = corpus
    token = login(client, documents_db, "niletech/finance-reader")
    stream = ask(client, token, FINANCE_Q, mode="documents")
    assert stream.done["outcome"] == "published", stream.raw[-800:]
    assert stream.done["verification"]["label"] == "answer_quotes_verified_dev_checked"
    assert stream.done["verification"]["inference_mode"] == "fake"
    # Only status events before publication; then text, citations, done.
    events = stream.events()
    first_content = min(i for i, e in enumerate(events) if e in ("token", "citation"))
    assert all(e == "status" for e in events[:first_content])
    (citation,) = [f.data for f in stream.of("citation")]
    assert citation["document_id"] == ids["finance-q1-commentary"]
    assert FINANCE_QUOTE_PART in citation["quote"]
    opened = open_citation(client, token, str(stream.run_id), citation["citation_id"])
    assert opened.status_code == 200
    body = opened.json()
    assert body["quote"] == citation["quote"]
    assert body["integrity"] == "verified" and body["offset_unit"] == "unicode_code_point"
    assert (body["char_start"], body["char_end"]) == (citation["char_start"], citation["char_end"])
    assert body["version"] == 1 and body["is_current_version"] is True
    canonical = owner_exec(
        documents_db,
        "SELECT canonical_text FROM document_versions WHERE document_id = :d AND version = 1",
        {"d": ids["finance-q1-commentary"]},
    ).scalar_one()
    assert canonical[body["char_start"] : body["char_end"]] == body["quote"]
    assert canonical[body["context_start"] : body["char_start"]] == body["before"]


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "What is the UAE leave entitlement in the table for الإمارات?",
            "UAE (الإمارات) | 30 | 30",
        ),
        ("Is Eid al-Fitr عيد الفطر deducted from approved leave?", "عيد الفطر"),
        ("Which customer is Gulf Ventures LLC 🚀 in Dubai?", "Gulf Ventures LLC \U0001f680"),
        ("How much family leave can parents take for caregiving?", "ten days of family leave"),
    ],
)
def test_citations_use_code_point_offsets_for_tables_and_non_ascii(
    corpus: Any, documents_db: DbHarness, model: Any, question: str, expected: str
) -> None:
    client, _, _ = corpus

    def answer(messages: list[dict[str, str]]) -> str:
        prompt = messages[1]["content"]
        import re

        for match in re.finditer(
            r'<source id="(s\d+)"[^\n]*>\n(.*?)\n</source>', prompt, flags=re.S
        ):
            body = match.group(2)
            for line in body.split("\n"):
                if expected in line:
                    quote = line.strip()
                    return json.dumps(
                        {
                            "answerable": True,
                            "sentences": [
                                {"text": quote, "cites": [match.group(1)], "quote": quote}
                            ],
                        }
                    )
        return json.dumps({"answerable": False})

    model(FakeModelClient({"answer": [answer]}))
    token = login(client, documents_db, "niletech/finance-reader")
    stream = ask(client, token, question, mode="documents")
    assert stream.done["outcome"] == "published", stream.raw[-600:]
    citation = stream.of("citation")[0].data
    opened = open_citation(client, token, str(stream.run_id), citation["citation_id"]).json()
    assert expected in opened["quote"] and opened["integrity"] == "verified"
    canonical = owner_exec(
        documents_db,
        "SELECT canonical_text FROM document_versions WHERE document_id = :d AND version = :v",
        {"d": citation["document_id"], "v": citation["version"]},
    ).scalar_one()
    assert canonical[opened["char_start"] : opened["char_end"]] == opened["quote"]


def test_repeated_passage_citation_points_at_the_cited_occurrence(
    corpus: Any, documents_db: DbHarness, model: Any
) -> None:
    client, ids, _ = corpus
    phrase = "Requests must be approved by the line manager."
    canonical = owner_exec(
        documents_db,
        "SELECT canonical_text FROM document_versions WHERE document_id = :d",
        {"d": ids["hr-leave-policy"]},
    ).scalar_one()
    occurrences = [i for i in range(len(canonical)) if canonical.startswith(phrase, i)]
    assert len(occurrences) == 2

    def answer_from(section: str) -> Callable[[list[dict[str, str]]], str]:
        def answer(messages: list[dict[str, str]]) -> str:
            import re

            for m in re.finditer(
                r'<source id="(s\d+)"([^\n]*)>\n(.*?)\n</source>',
                messages[1]["content"],
                flags=re.S,
            ):
                if section in m.group(2) and phrase in m.group(3):
                    return json.dumps(
                        {
                            "answerable": True,
                            "sentences": [{"text": phrase, "cites": [m.group(1)], "quote": phrase}],
                        }
                    )
            return json.dumps({"answerable": False})

        return answer

    token = login(client, documents_db, "niletech/finance-reader")
    starts = set()
    for section, question in (
        ("Entitlement", "Who must approve annual leave entitlement requests?"),
        ("Carry-over", "Who must approve carry-over requests for leave?"),
    ):
        model(FakeModelClient({"answer": [answer_from(section)]}))
        stream = ask(client, token, question, mode="documents")
        assert stream.done["outcome"] == "published", (section, stream.raw[-500:])
        citation = stream.of("citation")[0].data
        opened = open_citation(client, token, str(stream.run_id), citation["citation_id"]).json()
        assert opened["quote"] == phrase
        starts.add(opened["char_start"])
    assert starts == set(occurrences)


# -- T2, T3, T7: denied, cross-tenant, unknown permissions ----------------------------------


@pytest.mark.parametrize(
    "ref", ["niletech/sales-reader", "niletech/denied-reader", "isolation-test/finance-reader"]
)
def test_t2_t3_unauthorized_users_get_no_sources_and_no_markers(
    corpus: Any, documents_db: DbHarness, ref: str
) -> None:
    client, _, _ = corpus
    stream = ask_docs(client, documents_db, ref, FINANCE_Q)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["abstention"]["code"] == "no_sources"
    assert stream.events() == ["status", "status", "status", "done"]
    assert [f.data["step"] for f in stream.of("status")] == [
        "accepted",
        "authorization",
        "retrieval",
    ]
    assert_no_markers(stream.raw)
    assert "370 open invoices" not in stream.raw


def test_t3_isolation_tenant_reads_only_its_own_documents(
    corpus: Any, documents_db: DbHarness
) -> None:
    client, ids, _ = corpus
    stream = ask_docs(
        client,
        documents_db,
        "isolation-test/finance-reader",
        "When does the isolation finance team close the books?",
    )
    assert stream.done["outcome"] == "published", stream.raw[-500:]
    citation = stream.of("citation")[0].data
    assert citation["document_id"] == ids["isolation-handbook"]
    assert_no_markers(stream.raw, allowed=("ISOLATION-FINANCE-SECRET-5005",))


def test_t7_unknown_permissions_are_never_retrieved(corpus: Any, documents_db: DbHarness) -> None:
    client, ids, _ = corpus
    for ref in ("niletech/finance-reader", "niletech/board-member", "niletech/sales-reader"):
        stream = ask_docs(
            client,
            documents_db,
            ref,
            "Which customers should the Integration Hub prioritise in the draft strategy memo?",
        )
        assert "NILETECH-UNKNOWN-PERMISSION-4040" not in stream.raw
        for citation in stream.of("citation"):
            assert citation.data["document_id"] != ids["draft-strategy"]
    state = owner_exec(
        documents_db,
        "SELECT permission_state FROM resources WHERE id = :d",
        {"d": ids["draft-strategy"]},
    ).scalar_one()
    assert state == "unknown"


def test_t7_setting_permissions_makes_an_unknown_document_searchable(
    corpus: Any, documents_db: DbHarness
) -> None:
    client, ids, _ = corpus
    admin = login(client, documents_db, "niletech/access-admin")
    question = "Which product should be prioritised for Gulf customers per the draft strategy?"
    before = ask_docs(client, documents_db, "niletech/finance-reader", question)
    assert all(c.data["document_id"] != ids["draft-strategy"] for c in before.of("citation"))
    response = client.put(
        f"/api/v1/documents/{ids['draft-strategy']}/permissions",
        json={"classification": "internal", "grants": [{"group": "finance-team"}]},
        headers=auth(admin),
    )
    assert response.status_code == 204
    try:
        after = ask_docs(client, documents_db, "niletech/finance-reader", question)
        assert after.done["outcome"] == "published", after.raw[-500:]
        assert after.of("citation")[0].data["document_id"] == ids["draft-strategy"]
    finally:
        owner_exec(
            documents_db,
            "UPDATE resources SET permission_state = 'unknown' WHERE id = :d",
            {"d": ids["draft-strategy"]},
        )


# -- T4: revocation ------------------------------------------------------------------------


@pytest.fixture
def revoke_finance_team(documents_db: DbHarness) -> Iterator[Callable[[], None]]:
    tenant = documents_db.foundation.tenants["niletech"]
    group, user = tenant.groups["finance-team"].id, tenant.users["finance-reader"].id

    def revoke() -> None:
        owner_exec(
            documents_db,
            "DELETE FROM group_memberships WHERE group_id = :g AND user_id = :u",
            {"g": group, "u": user},
        )

    yield revoke
    owner_exec(
        documents_db,
        "INSERT INTO group_memberships (tenant_id, group_id, user_id)"
        " VALUES (:t, :g, :u) ON CONFLICT DO NOTHING",
        {"t": tenant.id, "g": group, "u": user},
    )


def test_t4_group_removal_applies_to_the_next_request(
    corpus: Any, documents_db: DbHarness, revoke_finance_team: Callable[[], None]
) -> None:
    client, _, _ = corpus
    token = login(client, documents_db, "niletech/finance-reader")
    assert ask(client, token, FINANCE_Q, mode="documents").done["outcome"] == "published"
    revoke_finance_team()
    stream = ask(client, token, FINANCE_Q, mode="documents")
    assert stream.done["abstention"]["code"] == "no_sources"
    assert "370 open invoices" not in stream.raw


@pytest.mark.parametrize("node", ["draft", "verify", "publish"])
def test_t4_revocation_during_a_run_prevents_publication(
    corpus: Any, documents_db: DbHarness, revoke_finance_team: Callable[[], None], node: str
) -> None:
    client, _, _ = corpus
    wf = workflow(client)

    def before(name: str, state: Any) -> None:
        if name == node:
            revoke_finance_team()

    wf.deps.before_node = before
    try:
        stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    finally:
        wf.deps.before_node = None
    assert stream.done["outcome"] == "failed"
    assert {f.data["code"] for f in stream.of("error")} == {"access_revoked"}
    assert not stream.of("token") and not stream.of("citation")
    assert "370 open invoices" not in stream.raw


def test_t4_t5_citation_and_replay_after_revocation_expose_nothing(
    corpus: Any, documents_db: DbHarness, revoke_finance_team: Callable[[], None]
) -> None:
    client, _, _ = corpus
    token = login(client, documents_db, "niletech/finance-reader")
    stream = ask(client, token, FINANCE_Q, mode="documents")
    cid = stream.of("citation")[0].data["citation_id"]
    revoke_finance_team()
    assert open_citation(client, token, str(stream.run_id), cid).status_code == 404
    from tests.integration.report_support import replay

    again = replay(client, token, str(stream.run_id))
    assert not again.of("token") and not again.of("citation")
    assert again.of("error")[-1].data["code"] == "access_revoked"
    run = client.get(f"/api/v1/runs/{stream.run_id}", headers=auth(token)).json()
    assert run["result"] is None
    assert "370 open invoices" not in again.raw + json.dumps(run)


def test_t5_guessed_document_and_citation_ids_are_not_found(
    corpus: Any, documents_db: DbHarness
) -> None:
    client, ids, _ = corpus
    owner = login(client, documents_db, "niletech/finance-reader")
    stream = ask(client, owner, FINANCE_Q, mode="documents")
    cid = stream.of("citation")[0].data["citation_id"]
    for ref in ("niletech/sales-reader", "isolation-test/finance-reader"):
        other = login(client, documents_db, ref)
        for path in (
            f"/api/v1/documents/{ids['finance-q1-commentary']}",
            f"/api/v1/documents/{ids['board-minutes']}",
            f"/api/v1/runs/{stream.run_id}/citations/{cid}",
            f"/api/v1/documents/{uuid.uuid4()}",
        ):
            response = client.get(path, headers=auth(other))
            assert response.status_code == 404, path
            assert_no_markers(response.text)
    listing = client.get(
        "/api/v1/documents", headers=auth(login(client, documents_db, "niletech/sales-reader"))
    )
    titles = {d["title"] for d in listing.json()}
    assert "Q1 2026 finance commentary" not in titles and "Board minutes, March 2026" not in titles
    assert "Annual leave policy 2026" in titles


# -- T8: no answer cache -------------------------------------------------------------------


def test_t8_repeated_question_after_revocation_is_not_served_from_a_cache(
    corpus: Any, documents_db: DbHarness, revoke_finance_team: Callable[[], None]
) -> None:
    """There is no answer cache in M3 (roadmap: disabled until revocation tests pass).
    The same question repeated after revocation is re-authorized and re-searched."""

    client, _, _ = corpus
    token = login(client, documents_db, "niletech/finance-reader")
    first = ask(client, token, FINANCE_Q, mode="documents", client_request_id="cache-probe-0001")
    assert first.done["outcome"] == "published"
    revoke_finance_team()
    second = ask(client, token, FINANCE_Q, mode="documents", client_request_id="cache-probe-0002")
    assert second.done["abstention"]["code"] == "no_sources"


# -- T9: the outbound Qdrant request carries the full filter --------------------------------


@pytest.mark.parametrize("mode", [RetrievalMode.DENSE, RetrievalMode.HYBRID])
def test_t9_every_search_branch_carries_the_eligible_id_filter(
    corpus: Any, documents_db: DbHarness, mode: RetrievalMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ids, settings = corpus
    wf = workflow(client)
    sent: list[tuple[str, str, dict[str, Any] | None]] = []
    wf.qdrant.on_request = lambda method, path, body: sent.append((method, path, body))
    monkeypatch.setattr(wf.deps, "settings", settings.model_copy(update={"retrieval_mode": mode}))
    try:
        stream = ask_docs(client, documents_db, "niletech/sales-reader", FINANCE_Q)
    finally:
        wf.qdrant.on_request = None
    queries = [b for m, p, b in sent if p.endswith("/points/query")]
    assert len(queries) == 1
    body = queries[0]
    assert body is not None
    branches = [body, *body.get("prefetch", [])]
    assert len(branches) == (3 if mode is RetrievalMode.HYBRID else 1)
    tenant = str(documents_db.foundation.tenants["niletech"].id)
    eligible = set(
        owner_exec(
            documents_db,
            "SELECT c.point_id::text FROM document_chunks c JOIN documents d ON d.id = c.document_id"
            " AND c.generation = d.active_generation WHERE d.id = ANY(CAST(:ids AS uuid[]))",
            {
                "ids": [
                    ids[k]
                    for k in (
                        "hr-leave-policy",
                        "it-security-handbook",
                        "product-catalogue",
                        "company-overview-pdf",
                        "newsletter-pdf",
                        "customers-csv",
                    )
                ]
            },
        )
        .scalars()
        .all()
    )
    forbidden = set(
        owner_exec(
            documents_db,
            "SELECT point_id::text FROM document_chunks WHERE document_id = ANY(CAST(:ids AS uuid[]))",
            {
                "ids": [
                    ids[k]
                    for k in (
                        "finance-q1-commentary",
                        "board-minutes",
                        "draft-strategy",
                        "sales-tables-pdf",
                        "q1-workbook",
                        "isolation-handbook",
                    )
                ]
            },
        )
        .scalars()
        .all()
    )
    for branch in branches:
        must = branch["filter"]["must"]
        assert {"key": "tenant_id", "match": {"value": tenant}} in must
        (has_id,) = [m["has_id"] for m in must if "has_id" in m]
        assert set(has_id) == eligible
        assert not set(has_id) & forbidden
    assert_no_markers(stream.raw)


def test_t9_no_query_is_sent_when_nothing_is_eligible(corpus: Any, documents_db: DbHarness) -> None:
    client, _, _ = corpus
    wf = workflow(client)
    sent: list[str] = []
    wf.qdrant.on_request = lambda method, path, body: sent.append(path)
    try:
        stream = ask_docs(client, documents_db, "niletech/access-admin", FINANCE_Q)
    finally:
        wf.qdrant.on_request = None
    assert stream.done["abstention"]["code"] == "no_sources"
    assert not [p for p in sent if p.endswith("/points/query")]


# -- T10: restricted versus removed ---------------------------------------------------------


def _normalize(stream: Stream) -> list[tuple[int | None, str, str]]:
    out = []
    for frame in stream.frames:
        data = {k: v for k, v in frame.data.items() if k not in ("run_id",)}
        out.append((frame.id, frame.event, json.dumps(data, sort_keys=True)))
    return out


def test_t10_restricted_and_removed_documents_are_indistinguishable(
    corpus: Any, documents_db: DbHarness
) -> None:
    """Same user, same question; the finance document is (a) present but not readable,
    (b) removed from the corpus. 30 interleaved runs each: status, non-volatile headers,
    event sequence and normalized payloads must match exactly, and median times must
    differ by less than 10%."""

    client, ids, _ = corpus
    doc = ids["finance-q1-commentary"]
    token = login(client, documents_db, "niletech/sales-reader")
    # Per-request ids (the run and, since M8, the saved conversation) differ every time.
    volatile = {"x-request-id", "x-run-id", "x-conversation-id", "date", "content-length"}

    def set_removed(removed: bool) -> None:
        owner_exec(
            documents_db,
            "UPDATE resources SET status = :s WHERE id = :d",
            {"s": "deleted" if removed else "active", "d": doc},
        )
        owner_exec(
            documents_db,
            "UPDATE documents SET deleted_at = CASE WHEN :r THEN now() END WHERE id = :d",
            {"r": removed, "d": doc},
        )

    def one(removed: bool) -> tuple[float, int, dict[str, str], list[Any]]:
        set_removed(removed)
        started = time.perf_counter()
        with client.stream(
            "POST",
            "/api/v1/chat",
            json={"question": FINANCE_Q, "mode": "documents"},
            headers=auth(token),
        ) as response:
            raw = "".join(response.iter_text())
            headers = {k: v for k, v in response.headers.items() if k.lower() not in volatile}
            status = response.status_code
        elapsed = time.perf_counter() - started
        from tests.integration.report_support import parse_sse

        return elapsed, status, headers, _normalize(Stream(raw, parse_sse(raw), None, status))

    try:
        for warm in (True, False):
            one(warm)
        restricted, removed = [], []
        for _ in range(30):
            restricted.append(one(False))
            removed.append(one(True))
    finally:
        set_removed(False)
    reference = restricted[0]
    for sample in restricted + removed:
        assert sample[1] == reference[1] == 200
        assert sample[2] == reference[2]
        assert sample[3] == reference[3]
    assert reference[3][-1][1] == "done"
    assert '"no_sources"' in reference[3][-1][2]
    med_restricted = statistics.median(s[0] for s in restricted)
    med_removed = statistics.median(s[0] for s in removed)
    difference = abs(med_restricted - med_removed) / max(med_restricted, med_removed)
    print(
        f"T10 medians: restricted={med_restricted * 1000:.1f} ms removed={med_removed * 1000:.1f} ms"
        f" difference={difference:.1%}"
    )
    assert difference < 0.10


# -- verification failures ----------------------------------------------------------------


def _quote_answer(text_: str, quote: str) -> str:
    return json.dumps(
        {"answerable": True, "sentences": [{"text": text_, "cites": ["s1"], "quote": quote}]}
    )


def test_fabricated_quote_and_number_are_repaired_or_abstained(
    corpus: Any, documents_db: DbHarness, model: Any
) -> None:
    client, _, _ = corpus
    fabricated = _quote_answer(
        "There were 999 overdue invoices.", "999 invoices were overdue at the reference date"
    )
    wrong_number = _quote_answer(
        "There were 371 overdue invoices.",
        "Collections lagged revenue because 370 open invoices were already past their due date",
    )
    model(FakeModelClient({"answer": [fabricated, wrong_number, fabricated]}))
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.done["outcome"] == "abstained"
    assert stream.done["abstention"]["code"] == "verification_failed"
    for _, _, payload in _normalize(stream):
        assert "999" not in payload and "371" not in payload
    assert not stream.of("token")


def test_one_repair_then_published(corpus: Any, documents_db: DbHarness, model: Any) -> None:
    client, _, _ = corpus
    model(
        FakeModelClient(
            {
                "answer": [
                    "not json",
                    _quote_answer(
                        "370 open invoices were past their due date.",
                        "370 open invoices were already past their due date at the reference date.",
                    ),
                ]
            }
        )
    )
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.done["outcome"] == "published", stream.raw[-500:]
    retry = [f for f in stream.of("status") if f.data["state"] == "retrying"]
    assert retry and retry[0].data["attempt"] == 2


def test_checker_rejection_and_unavailability_abstain(
    corpus: Any, documents_db: DbHarness, model: Any
) -> None:
    client, _, _ = corpus
    model(FakeModelClient(checker=lambda p, h: CheckResult(0.01, 0.0, 0.99)))
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.done["abstention"]["code"] == "verification_failed"
    assert not stream.of("token")
    model(FakeModelClient(checker=GatewayUnavailable("down")))
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.done["abstention"]["code"] == "verification_failed"


def test_model_unanswerable_uses_the_no_sources_template(
    corpus: Any, documents_db: DbHarness, model: Any
) -> None:
    client, _, _ = corpus
    model(FakeModelClient({"answer": ['{"answerable": false}']}))
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.done["abstention"]["code"] == "no_sources"


def test_embedding_unavailable_fails_clearly(
    corpus: Any, documents_db: DbHarness, model: Any
) -> None:
    client, _, _ = corpus
    model(FakeModelClient(embed_error=GatewayUnavailable("down")))
    stream = ask_docs(client, documents_db, "niletech/finance-reader", FINANCE_Q)
    assert stream.of("error")[0].data["code"] == "model_unavailable"
    assert_no_markers(stream.raw)


def test_auto_mode_routes_questions(corpus: Any, documents_db: DbHarness) -> None:
    client, _, _ = corpus
    token = login(client, documents_db, "niletech/finance-reader")
    documents = ask(client, token, FINANCE_Q)
    assert documents.done["outcome"] == "published"
    assert documents.of("citation")[0].data.get("document_id")
    run = owner_exec(
        documents_db, "SELECT kind FROM report_runs WHERE id = :i", {"i": documents.run_id}
    ).scalar_one()
    assert run == "documents"
