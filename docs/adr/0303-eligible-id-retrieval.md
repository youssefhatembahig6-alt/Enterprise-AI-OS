# ADR 0303 — Authorization inside retrieval (eligible point ids)

Status: accepted for M3, pending planning-agent review. Completes [ADR 0104](0104-retrieval-eligible-ids-design.md) (proposed design) and source ADR 0004/0012. Source: specification §8 "Permission filtering in Qdrant", "Consistency boundary", T9; roadmap "Stale index permissions". Implementation: `backend/app/documents/access.py`, `qdrant.py`, `answers.py`.

## Decision

1. **Eligibility from the reference policy itself.** For each question, one transaction reads the tenant policy epoch (E0), loads the caller's current subject and the metadata and grants of every searchable document (active resource, not deleted, active generation), and evaluates `resource.read` with the same Python evaluator used everywhere else. The allowed documents' active-generation point ids form the eligible set. ADR 0104 proposed doing this in SQL plus an equivalence test. Evaluating in Python over PostgreSQL state makes the search filter and the policy the same code, so there is nothing to keep equivalent. This scales to the project's small corpus; a compiled payload filter would need its own equivalence test.
2. **Filter on every branch.** `{"must": [{"key": "tenant_id", "match": T}, {"has_id": eligible}]}` is attached to the dense query, or to both hybrid prefetches (dense, sparse) **and** the RRF fusion stage. An empty set sends no query and returns the ordinary no-sources response.
3. **Recheck before text.** Hits are rechecked in a new transaction: current decision, document not deleted, the hit's generation still active. Only then is chunk text loaded, from PostgreSQL; Qdrant holds no text.
4. **Recheck before publication.** Inside the M2 publication boundary (tenant policy-epoch share lock), cited documents are locked `FOR SHARE` and access and active generation are rechecked. Content events, answer sources and the result are written in that transaction.
5. **Streams and citations.** Content events are re-authorized per event (`resource.read` on every cited document, not deleted); citations re-check on every open.
6. **Missing collection** (nothing indexed in the current embedding space yet) finds nothing rather than erroring.

## Guarantee and limits

The search request named only point ids eligible at E0, and every passage shown was eligible at the recheck and at publication. This does not claim that Qdrant never scored other points, and a revocation cannot recall content already sent. There is no answer cache in M3 (T8: not applicable; the roadmap disables caching until revocation tests pass).

## Evidence

`tests/integration/test_documents_answers.py::test_t9_every_search_branch_carries_the_eligible_id_filter[dense|hybrid]` asserts on the captured outbound request: tenant match, and `has_id` equal to the eligible set and disjoint from forbidden documents' points, on every branch. `::test_t9_no_query_is_sent_when_nothing_is_eligible`; T1–T5, T7 and T8 cases in the same file.
