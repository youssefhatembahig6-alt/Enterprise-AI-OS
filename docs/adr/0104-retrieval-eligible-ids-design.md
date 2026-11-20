# ADR 0104 — Eligible-ID retrieval (design only; nothing implemented in M0)

Status: **proposed**. Proposes how to complete ADR 0004 (Decided) and ADR 0012 (**Proposed** in the source). Source: [specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f) §8 "Permission filtering in Qdrant" and "Consistency boundary"; roadmap "Stale index permissions".

## Problem

A Qdrant payload ACL can be stale. A later PostgreSQL recheck can stop content loading, but it cannot prove that obsolete candidates were never retrieved. The source claim that stale payloads "can only return fewer results" holds only if the filter is derived from current state.

## Proposal (for the small project corpus)

1. **Snapshot.** Each search opens a PostgreSQL transaction under the user's tenant context, reads `tenants.policy_epoch` (E₀) and builds the access context.
2. **Eligible set.** In SQL, evaluate the reference policy (ADR 0103) over resources that are active, verified, not deleted and in the active index generation, and return their **active point IDs**. The SQL must be checked for equivalence against the Python evaluator, using the fixture grid on every path (the source's equivalence test).
3. **Empty set.** If the set is empty, return the single no-sources response immediately. Never send an unrestricted query, and never run a different code path that would show a denial (FR-04).
4. **Constrained query.** Send Qdrant a filter with `must: [tenant_id == T, has_id: [eligible ids]]` on **every** branch: dense, sparse, each prefetch and fusion input, not only the final stage. Qdrant holds vectors plus minimum indexing metadata; titles and text stay in PostgreSQL. Test T9 asserts the outbound request.
5. **Recheck before content leaves the backend.** Load candidate text from PostgreSQL in a new transaction, re-evaluate the policy for each candidate and read the epoch (E₁). If E₁ ≠ E₀, either re-run steps 2–5 once, or drop candidates that no longer pass. If freshness cannot be established, fail closed (no sources).
6. **Before publication** (answer verification, M2/M3), recheck the cited resources at the then-current epoch.

## Version and race semantics

- The guarantee is: *the search request named only IDs that were eligible at epoch E₀, and every passage shown was eligible at the recheck epoch.* It does not claim that Qdrant never scored other points.
- A revocation that commits after E₀ affects results at the step-5 recheck. A revocation after content has been sent cannot be recalled. This is documented, not hidden.
- Point identity includes the index generation, so a rechunk does not overwrite the active generation before publication.

## Limits

An ID list is practical for hundreds to low thousands of points. Larger corpora need a compiled payload filter, and must keep the same equivalence test before replacing this approach.
