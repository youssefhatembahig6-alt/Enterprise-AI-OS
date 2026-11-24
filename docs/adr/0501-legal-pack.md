# ADR 0501 — Legal pack: clause comparison and expiry answers

Status: Accepted for M5 (pending planning review). Relates to: FR-06, FR-03, FR-09, roadmap "M4 and M5 connector and UI details" (legal outputs require citations for comparisons and expiry facts). Implementation: `backend/app/legal/extract.py`, `backend/app/legal/workflow.py`.

## Decision

1. **A pack on the M3 path.** Run kind `legal`, pack `legal` (tenant configuration, ADR 0403). The run, event, publication and replay contract is the document workflow's (ADR 0303/0304). Contracts are ordinary documents with M0 permissions; the pack searches every non-repository source (upload, Drive, local folder), never GitHub code.
2. **Only authorized texts are analysed.** Eligible documents are computed at policy epoch E0 with the reference policy. In a new transaction each one is rechecked (current `resource.read`, not deleted, active generation) **before** its canonical text is loaded. A contract the caller cannot read is never loaded, so it cannot contribute a clause, a date or its title. Publication rechecks every cited document under the policy-epoch lock (M3), so a revocation between drafting and publication publishes nothing (`access_revoked`).
3. **Deterministic extraction, no model.** Clauses are the paragraphs under recognised headings (termination, liability, payment, confidentiality, governing law, term, indemnity, warranty, data protection, service levels). Facts are tied to the exact code-point span of the sentence they come from: effective date, explicit expiry date, term length, renewal period and renewal notice. Comparable clause values: notice days, payment days, liability cap text, governing law, confidentiality survival. Unrecognised wording yields no fact rather than a guess. English only (initial acceptance language).
4. **Conventions, stated in the answer.** A term of N months starting on day D ends on the day before D + N months (1 April 2025 + 24 months → 31 March 2027). An explicit expiry date wins over a computed one. A renewal notice deadline is the expiry minus the notice days.
5. **Every value is re-derived from its quotes.** Verification reloads each cited version, checks its text hash, re-slices each quote by its offsets and re-runs the extractors on the quote alone: an explicit date must be in its quote; a computed expiry must equal effective date + term from the two cited quotes; a renewal deadline must equal the recomputed expiry minus the cited notice days; a clause value must re-extract from its quote, and every number in a non-derived statement must appear in it. A comparison sentence cites every compared passage. Label: `legal_facts_extracted_and_rechecked`, `model_used: false`.
6. **Names and refusals.** A question that names contracts is answered for the named readable contracts only. A name that matches no readable title yields the M3 `no_sources` template, which is identical whether the contract is unreadable or does not exist.

## Consequences

- Legal results are complete and reproducible in every model mode (no Colab dependency), and the fake-model label never stands in for a legal result.
- Coverage is limited to the recognised wording; a clause written differently is answered with its quoted first sentence ("see the quoted clause") or not at all. A model-written summary may be added later behind the same verification; it is not part of M5.

## Evidence

`backend/tests/unit/test_m5_legal_extract.py` (hand-computed reference in `fixtures/legal/legal.toml`, date conventions, wording variants) and `backend/tests/integration/test_m5_legal.py` (exact passages, comparison citations, unreadable contract excluded, indistinguishable refusal, revocation at publication, routing, pack enforcement).
