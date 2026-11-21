# ADR 0304 — Exact citations and indistinguishable refusals

Status: accepted for M3, pending planning-agent review. Source: specification §9 citations, §8 "Indistinguishable refusals", FR-03, FR-04, T10; roadmap "Exact citations" and the M3 refusal comparison. Implementation: `backend/app/documents/answers.py`, `backend/app/reporting/service.py::citation`, `apps/web/src/ReportPanel.tsx`.

## Decision

- **Citations bind to a version.** A citation stores document, version, generation, point, code-point start (inclusive) and end (exclusive), the canonical text's SHA-256 and the quote's SHA-256, immutably (`answer_sources`). Opening it reloads that version's canonical text, rechecks both hashes and current access, and returns `before` / `quote` / `after` sliced on the server. JavaScript strings index UTF-16 code units, so the browser never converts offsets.
- **Quotes are verified, not trusted.** The model must quote a cited passage. The quote is located inside the cited chunk's span (tolerating only whitespace differences) and stored as the exact canonical substring. Repeated passages therefore resolve to the occurrence inside the cited chunk. Numbers in a sentence must appear in its quote.
- **One no-sources template.** Nothing relevant, not permitted, nothing eligible, and "the model found the passages insufficient" all end with the same events and the same `no_sources` message. The same eligibility computation and search run in every case; there is no extra lookup on denial. Private reasons go to run stats and the admin-only audit log.
- **Comparison method (T10).** The same user asks the same question 30 times while the document is present but unreadable, interleaved with 30 times while it is removed (tombstoned). Volatile fields are normalized: `run_id` in payloads, and the `x-request-id`, `x-run-id`, `date` and `content-length` headers. Status, remaining headers, event ids and types, and payloads must match exactly. Median wall times must differ by less than 10%.
- **Retrieval defaults to dense with the lexical relevance filter.** On 22 frozen development questions and the same fresh BGE-M3 index, dense and hybrid each reached 0.909 recall@5. Dense had higher MRR@5 (0.886 versus 0.841), with two rank-1 hits moving to rank 2 under hybrid. The lexical gate kept all 20 retrieved gold passages in dense mode and 28 of 51 hits from other documents. The diagnostic study exposed eight dropped paraphrases and nine irrelevant pairs that passed. A cosine threshold of 0.5298 preserved all relevant diagnostic pairs and dropped two corpus gold hits. The relevance filter narrows context after authorization. The quote and entailment checks still gate publication. [M3 evidence](../evidence/m3/README.md)

## Evidence

`tests/integration/test_documents_answers.py`:
- `test_t1_authorized_answer_with_exact_citation`;
- `test_citations_use_code_point_offsets_for_tables_and_non_ascii` (Arabic table row, Arabic text, emoji, plain);
- `test_repeated_passage_citation_points_at_the_cited_occurrence`;
- `test_t10_restricted_and_removed_documents_are_indistinguishable` (latest run: medians 134.1 ms vs 129.5 ms, 3.4%);
- `test_new_version_activates_and_old_citations_open_the_cited_version` (ingestion suite);
- unit tests on code-point counting (a 22-code-point string that is 25 UTF-16 units).
