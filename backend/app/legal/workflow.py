"""Legal pack workflow (M5, ADR 0501): clause comparison and expiry answers.

    prepare -> retrieve -> draft -> verify -> publish -> finish

The graph, run/event contract and publication boundary are the document workflow's
(M3). What differs:

* **Retrieve** computes the caller's eligible documents at policy epoch E0 with the
  reference policy (non-repository sources only), then, in a new transaction, rechecks
  each one (current ``resource.read``, not deleted, active generation) before loading
  its canonical text. Only those texts are analysed. A contract the caller cannot read
  is never loaded, so it cannot contribute a clause, a date or even its title.
* **Draft** is deterministic (no model): expiry facts and clause values are extracted
  from the authorized texts (``app.legal.extract``) and every sentence cites the exact
  sentence spans it relies on.
* **Verify** reloads each cited version, re-slices every quote by its code-point
  offsets, and re-derives each published value from the quotes alone: an explicit
  date must be in its quote, a computed expiry must equal effective date + term from
  the two cited quotes, a renewal deadline must equal expiry minus the cited notice
  days, and a clause value must re-extract from its quote. Numbers in non-derived
  statements must appear in the quotes.
* **Publish** reuses the M3 boundary: policy-epoch lock, cited documents locked
  ``FOR SHARE``, access and active generation rechecked, immutable answer sources.

No-sources behaviour is the M3 template: the same status events and the same message
whether no contract exists, none is readable, or none has what was asked.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.documents import access, store
from app.documents.answers import DocState, DocumentAnswerWorkflow, significant_terms
from app.legal import extract as lx
from app.reporting.contracts import AbstentionCode, Step
from app.reporting.model_client import FAKE_LABEL

VERIFICATION_LABEL = "legal_facts_extracted_and_rechecked"
MAX_CONTRACTS = 4
MAX_QUOTE = 400
GENERIC_TITLE_WORDS = frozenset(
    {
        "agreement",
        "contract",
        "master",
        "services",
        "service",
        "supply",
        "licence",
        "license",
        "enterprise",
        "software",
        "logistics",
        "the",
        "and",
    }
)
EXPIRY_Q = re.compile(
    r"\b(expir\w*|renew\w*|end\s+date|ends?\b|ending|notice\s+deadline|valid\s+until|"
    r"run\s+out|term\s+end\w*)",
    re.I,
)
COMPARE_Q = re.compile(r"\b(compar\w*|differ\w*|versus|vs\.?|between|both|each)\b", re.I)
BEFORE_Q = re.compile(r"\b(?:before|by|prior\s+to)\s+(?P<date>.+?)(?:\?|$)", re.I)
AFTER_Q = re.compile(r"\bafter\s+(?P<date>.+?)(?:\?|$)", re.I)
YEAR_Q = re.compile(r"\b(?:in|during)\s+(?P<year>\d{4})\b", re.I)
CONVENTION = "a term of N months starting on day D ends on the day before D + N months"


def legal_intent(question: str) -> dict[str, Any]:
    kinds = lx.question_clause_kinds(question)
    wants_expiry = bool(EXPIRY_Q.search(question))
    clause_kinds = [k for k in kinds if k != "term"]
    if wants_expiry and not (COMPARE_Q.search(question) and clause_kinds):
        intent = "expiry"
    elif clause_kinds:
        intent = "clauses"
    elif "term" in kinds:
        intent = "expiry"
    else:
        intent = "unknown"
    before = after = None
    year = None
    if m := BEFORE_Q.search(question):
        before = _first_date(m.group("date"))
    if m := AFTER_Q.search(question):
        after = _first_date(m.group("date"))
    if m := YEAR_Q.search(question):
        year = int(m.group("year"))
    return {
        "intent": intent,
        "clause_kinds": clause_kinds[:2],
        "before": before,
        "after": after,
        "year": year,
    }


def _first_date(value: str) -> date | None:
    match = lx.DATE_RE.search(value)
    if match:
        return lx.parse_date(match)
    if year := re.fullmatch(r"\s*(\d{4})\s*", value):
        return date(int(year.group(1)), 1, 1)
    return None


def named_contracts(question: str, titles: dict[str, str]) -> tuple[list[str], bool]:
    """Documents whose distinctive title words appear in the question, and whether the
    question names something (a capitalised word other than the first) that matches no
    readable title. The caller answers nothing for an unmatched name, which is the same
    whether that contract is unreadable or does not exist."""

    terms = significant_terms(question)
    matched, title_terms = [], set()
    for doc_id, title in titles.items():
        distinctive = significant_terms(title) - GENERIC_TITLE_WORDS
        title_terms |= distinctive
        if distinctive & terms:
            matched.append(doc_id)
    words = re.findall(r"\b[A-Z][\w-]{2,}\b", question)[1:]
    named = {
        t
        for w in words
        for t in significant_terms(w)
        if t not in GENERIC_TITLE_WORDS and not lx.question_clause_kinds(w)
    }
    unmatched = bool(named) and not (named & title_terms)
    return matched, unmatched


class LegalAnswerWorkflow(DocumentAnswerWorkflow):
    kind = "legal"

    # -- retrieve: authorized contract texts only ------------------------------------------

    def retrieve(self, state: DocState) -> dict[str, Any]:
        user_id = state["user_id"]
        with self._tx(state) as conn:
            elig = access.eligible(conn, user_id, self.kind)
        contracts: list[dict[str, Any]] = []
        dropped: dict[str, int] = {}
        with self._tx(state) as conn:  # recheck in a new transaction, before any text
            for document_id in elig.document_ids:
                doc = store.load_document(conn, document_id)
                if doc is None or doc.deleted_at is not None or doc.active_generation is None:
                    dropped["document_changed"] = dropped.get("document_changed", 0) + 1
                    continue
                if not access.can_read(conn, user_id, document_id).allowed:
                    dropped["access_recheck"] = dropped.get("access_recheck", 0) + 1
                    continue
                version_no = conn.execute(
                    text(
                        "SELECT version FROM document_generations"
                        " WHERE document_id = :d AND generation = :g AND status = 'active'"
                    ),
                    {"d": document_id, "g": doc.active_generation},
                ).scalar_one_or_none()
                version = (
                    store.load_version(conn, document_id, int(version_no)) if version_no else None
                )
                if version is None or version.canonical_text is None:
                    continue
                facts = lx.analyse(version.canonical_text, version.elements)
                if not facts.is_contract:
                    continue
                contracts.append(
                    {
                        "doc": doc,
                        "version": version,
                        "generation": doc.active_generation,
                        "facts": facts,
                    }
                )
        stats = self._stats_for(state)
        stats["retrieval"] = {
            "policy_epoch": elig.policy_epoch,
            "eligible_documents": len(elig.document_ids),
            "contracts": len(contracts),
            "dropped": dropped,
            "method": "authorized_full_text_scan",
        }
        with self._tx(state) as conn:
            self._audit(
                conn,
                state,
                event_type="document.retrieve",
                action="resource.read",
                decision="info",
                reason="searched",
                details={
                    "run_id": str(state["run_id"]),
                    "eligible_documents": len(elig.document_ids),
                    "denied_documents": len(elig.denied),
                    "contracts": len(contracts),
                    "policy_epoch": elig.policy_epoch,
                    "pack": self.kind,
                },
            )
        self._status(state, Step.RETRIEVAL, "completed")
        if not contracts:
            private = "no_eligible_documents" if not elig.document_ids else "no_contracts"
            return {"terminal": self._abstain(AbstentionCode.NO_SOURCES, private), "passages": []}
        plan = self._plan(state["question"], contracts)
        if plan is None:
            return {
                "terminal": self._abstain(AbstentionCode.NO_SOURCES, "no_matching_facts"),
                "passages": [],
            }
        passages, sentences = plan
        with self._tx(state) as conn:
            for passage in passages:  # each cited span is anchored to an active chunk
                point = conn.execute(
                    text(
                        "SELECT point_id FROM document_chunks WHERE document_id = :d"
                        " AND generation = :g AND char_start <= :s AND char_end > :s"
                        " ORDER BY char_start LIMIT 1"
                    ),
                    {
                        "d": passage["document_id"],
                        "g": passage["generation"],
                        "s": passage["char_start"],
                    },
                ).scalar_one_or_none()
                if point is None:
                    return {
                        "terminal": self._abstain(AbstentionCode.NO_SOURCES, "span_not_indexed"),
                        "passages": [],
                    }
                passage["point_id"] = str(point)
        return {"passages": passages, "planned": sentences}

    # -- planning (deterministic drafting) -------------------------------------------------

    def _plan(  # noqa: PLR0915 - one deterministic drafting table
        self, question: str, contracts: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        intent = legal_intent(question)
        titles = {str(c["doc"].id): c["doc"].title for c in contracts}
        matched, unmatched = named_contracts(question, titles)
        if unmatched and not matched:
            return None
        named = set(matched)
        chosen = [c for c in contracts if not named or str(c["doc"].id) in named]
        chosen.sort(key=lambda c: (c["doc"].title, str(c["doc"].id)))
        passages: list[dict[str, Any]] = []
        sentences: list[dict[str, Any]] = []

        def cite(contract: dict[str, Any], span: lx.Span, heading: str) -> str:
            doc, version = contract["doc"], contract["version"]
            if span.end - span.start > MAX_QUOTE:
                span = lx.Span(span.start, span.start + MAX_QUOTE, span.text[:MAX_QUOTE])
            for p in passages:
                if p["document_id"] == str(doc.id) and (p["char_start"], p["char_end"]) == (
                    span.start,
                    span.end,
                ):
                    return str(p["source_id"])
            sid = f"s{len(passages) + 1}"
            passages.append(
                {
                    "source_id": sid,
                    "point_id": "",
                    "document_id": str(doc.id),
                    "title": doc.title,
                    "version": version.version,
                    "generation": contract["generation"],
                    "char_start": span.start,
                    "char_end": span.end,
                    "page": None,
                    "heading_path": [heading] if heading else [],
                    "prefix": "",
                    "span_text": span.text,
                    "text_sha256": version.text_sha256,
                    "score": 1.0,
                    "source": doc.source,
                    "source_revision": version.source_revision,
                    "verbatim_lines": False,
                    "line_at_start": 0,
                }
            )
            return sid

        if intent["intent"] == "expiry":
            rows = []
            for contract in chosen:
                expiry = lx.expiry_of(contract["facts"])
                if expiry is None:
                    continue
                when: date = expiry["date"]
                if intent["before"] and not when < intent["before"]:
                    continue
                if intent["after"] and not when > intent["after"]:
                    continue
                if intent["year"] and when.year != intent["year"]:
                    continue
                rows.append((when, contract, expiry))
            for when, contract, expiry in sorted(rows, key=lambda r: (r[0], r[1]["doc"].title))[
                :MAX_CONTRACTS
            ]:
                facts: dict[str, lx.Fact] = contract["facts"].facts
                term_heading = (
                    contract["facts"].clause("term") or lx.Clause("", "", lx.Span(0, 0, ""))
                ).heading
                if expiry["derived"]:
                    eff, term = facts["effective"], facts["term_months"]
                    sids = [
                        cite(contract, eff.span, term_heading),
                        cite(contract, term.span, term_heading),
                    ]
                    statement = (
                        f"the initial term ends on {lx.show_date(when)} ({term.value} months from "
                        f"{lx.show_date(eff.value)}, counting the last day of the term)"
                    )
                    check: dict[str, Any] = {
                        "rule": "effective_plus_term",
                        "value": when.isoformat(),
                        "effective": sids[0],
                        "term": sids[1],
                    }
                else:
                    sids = [cite(contract, facts["expiry"].span, term_heading)]
                    statement = f"expires on {lx.show_date(when)}"
                    check = {"rule": "explicit_expiry", "value": when.isoformat(), "quote": sids[0]}
                sentences.append(self._sentence(contract, statement, sids, check))
                renewal, notice = facts.get("renewal_months"), facts.get("renewal_notice_days")
                if renewal is not None and notice is not None:
                    deadline = date.fromordinal(when.toordinal() - int(notice.value))
                    rsid = cite(contract, notice.span, term_heading)
                    statement = (
                        f"it renews automatically for {renewal.value} months unless notice is "
                        f"given at least {notice.value} days before the term ends, that is by "
                        f"{lx.show_date(deadline)}"
                    )
                    rcheck = {
                        "rule": "renewal_deadline",
                        "value": deadline.isoformat(),
                        "renewal_months": int(renewal.value),
                        "notice_days": int(notice.value),
                        "quote": rsid,
                        "expiry": check,
                    }
                    sentences.append(self._sentence(contract, statement, [*sids, rsid], rcheck))
            return (passages, sentences) if sentences else None

        if intent["intent"] == "clauses":
            for kind in intent["clause_kinds"]:
                label = lx.CLAUSE_LABELS[kind]
                compared: list[tuple[dict[str, Any], lx.Attribute, str]] = []
                for contract in chosen[:MAX_CONTRACTS]:
                    clause = contract["facts"].clause(kind)
                    if clause is None:
                        continue
                    text_ = contract["version"].canonical_text
                    attribute = lx.clause_attribute(text_, clause)
                    if attribute is not None:
                        sid = cite(contract, attribute.span, clause.heading)
                        check = {
                            "rule": "attribute",
                            "kind": kind,
                            "name": attribute.name,
                            "value": attribute.value,
                            "quote": sid,
                        }
                        statement = f"{label.lower()}: {attribute.display}"
                        sentences.append(self._sentence(contract, statement, [sid], check))
                        compared.append((contract, attribute, sid))
                    else:
                        first = lx.sentences(text_, clause.span.start, clause.span.end)[0]
                        sid = cite(contract, first, clause.heading)
                        statement = f"{label.lower()}: see the quoted clause"
                        check = {"rule": "quote_only", "quote": sid}
                        sentences.append(self._sentence(contract, statement, [sid], check))
                numeric = [c for c in compared if isinstance(c[1].value, int)]
                if len(numeric) >= 2 and len({a.name for _, a, _ in numeric}) == 1:
                    ordered = sorted(numeric, key=lambda c: (-c[1].value, c[0]["doc"].title))
                    hi, lo = ordered[0], ordered[-1]
                    unit = "days" if numeric[0][1].name.endswith("_days") else "years"
                    if hi[1].value == lo[1].value:
                        statement = (
                            f"All {len(numeric)} compared contracts set the same {label.lower()} "
                            f"value: {hi[1].value} {unit}"
                        )
                    else:
                        statement = (
                            f"Longest {label.lower()} period: {hi[0]['doc'].title} "
                            f"({hi[1].value} {unit}); shortest: {lo[0]['doc'].title} "
                            f"({lo[1].value} {unit})"
                        )
                    check = {
                        "rule": "comparison",
                        "kind": kind,
                        "name": numeric[0][1].name,
                        "values": {sid: a.value for _, a, sid in numeric},
                    }
                    sentences.append(
                        {
                            "text": statement + ".",
                            "statement": statement,
                            "cites": [sid for _, _, sid in numeric],
                            "check": check,
                            "titled": False,
                        }
                    )
            return (passages, sentences) if sentences else None
        return None

    @staticmethod
    def _sentence(
        contract: dict[str, Any], statement: str, sids: list[str], check: dict[str, Any]
    ) -> dict[str, Any]:
        title = contract["doc"].title
        return {
            "text": f"{title}: {statement}.",
            "statement": statement,
            "cites": list(dict.fromkeys(sids)),
            "check": check,
            "titled": True,
        }

    # -- draft / verify --------------------------------------------------------------------

    def draft(self, state: DocState) -> dict[str, Any]:
        attempt = state.get("answer_attempt", 0) + 1
        self._status(state, Step.ANSWER_DRAFTING, "completed", attempt=attempt)
        return {
            "answer_attempt": attempt,
            "sentences": list(state.get("planned") or []),
            "answer_feedback": [],
        }

    def verify(self, state: DocState) -> dict[str, Any]:
        by_id = {p["source_id"]: p for p in state["passages"]}
        quotes: dict[str, str] = {}
        feedback: list[str] = []
        with self._tx(state) as conn:
            for sid, passage in by_id.items():
                version = store.load_version(conn, UUID(passage["document_id"]), passage["version"])
                if version is None or version.text_sha256 != passage["text_sha256"]:
                    feedback.append(f"{sid}: cited version changed")
                    continue
                exact = (version.canonical_text or "")[passage["char_start"] : passage["char_end"]]
                if exact != passage["span_text"]:
                    feedback.append(f"{sid}: quote is not the cited span")
                    continue
                quotes[sid] = exact
        for index, sentence in enumerate(state["sentences"], start=1):
            if any(c not in quotes for c in sentence["cites"]):
                feedback.append(f"sentence {index}: unknown or changed source")
                continue
            problem = self._check(sentence, quotes)
            if problem:
                feedback.append(f"sentence {index}: {problem}")
        if feedback:
            for item in feedback:
                self._stat(state, "rejections", "legal:" + item.split(": ", 1)[-1])
            return {"answer_feedback": feedback}
        self._status(state, Step.ANSWER_VERIFICATION, "completed")
        return {"answer_feedback": []}

    @staticmethod
    def _check(sentence: dict[str, Any], quotes: dict[str, str]) -> str | None:
        check = sentence["check"]
        rule = check["rule"]

        def facts(sid: str) -> dict[str, Any]:
            return lx.sentence_facts(lx.Span(0, len(quotes[sid]), quotes[sid]))

        def expiry_ok(c: dict[str, Any]) -> bool:
            if c["rule"] == "explicit_expiry":
                return facts(c["quote"]).get("expiry") == date.fromisoformat(c["value"])
            eff = facts(c["effective"]).get("effective")
            months = facts(c["term"]).get("term_months")
            return (
                eff is not None
                and months is not None
                and lx.term_end(eff, months) == date.fromisoformat(c["value"])
            )

        if rule in ("explicit_expiry", "effective_plus_term"):
            return None if expiry_ok(check) else "date is not derivable from the quotes"
        if rule == "renewal_deadline":
            found = facts(check["quote"])
            if found.get("renewal_months") != check["renewal_months"]:
                return "renewal period not in the quote"
            if found.get("renewal_notice_days") != check["notice_days"]:
                return "notice period not in the quote"
            if not expiry_ok(check["expiry"]):
                return "expiry is not derivable from the quotes"
            expiry = date.fromisoformat(check["expiry"]["value"])
            deadline = date.fromordinal(expiry.toordinal() - check["notice_days"])
            return None if deadline.isoformat() == check["value"] else "deadline mismatch"
        if rule == "attribute":
            quote = quotes[check["quote"]]
            got = lx.attribute_in(check["kind"], lx.Span(0, len(quote), quote))
            if got is None or got.value != check["value"]:
                return "clause value does not re-extract from the quote"
            if not lx.numbers(sentence["statement"]) <= lx.numbers(quote):
                return "number not in the quote"
            return None
        if rule == "comparison":
            for sid, value in check["values"].items():
                quote = quotes[sid]
                got = lx.attribute_in(check["kind"], lx.Span(0, len(quote), quote))
                if got is None or got.value != value or got.name != check["name"]:
                    return "compared value does not re-extract"
            return None
        if rule == "quote_only":
            return None if quotes[check["quote"]].strip() else "empty quote"
        return "unknown check"

    # -- publication -----------------------------------------------------------------------

    def build_answer(
        self, state: DocState
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        by_id = {p["source_id"]: p for p in state["passages"]}
        cited: dict[str, dict[str, Any]] = {}
        by_source: dict[str, str] = {}
        answer = []
        for sentence in state["sentences"]:
            cids = []
            for sid in sentence["cites"]:
                if sid not in by_source:
                    cid = f"c{len(cited) + 1}"
                    passage = by_id[sid]
                    cited[cid] = self.citation(
                        cid, passage, passage["char_start"], passage["char_end"]
                    )
                    by_source[sid] = cid
                cids.append(by_source[sid])
            answer.append({"text": sentence["text"], "citations": cids})
        return answer, cited

    def verification(self, state: DocState, mode: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "label": VERIFICATION_LABEL,
            "method": "deterministic_extraction_no_model",
            "quotes": "exact_spans_of_cited_versions",
            "values": "re_derived_from_cited_quotes",
            "conventions": [CONVENTION, "renewal deadline = expiry minus notice days"],
            "access": "rechecked_at_publication",
            "inference_mode": mode,
            "model_used": False,
            "checker": None,
        }
        if mode == "fake":
            out["inference_label"] = FAKE_LABEL
        return out
