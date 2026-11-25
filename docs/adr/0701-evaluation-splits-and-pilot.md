# ADR 0701: Family-level frozen evaluation splits and a separate pilot

Status: Proposed for M7 (pending planning review)

## Context

The roadmap requires four distinct datasets and a frozen document/template-family split before derived claims are generated, so that the same template cannot contaminate train and held-out sets. Before M7 all evaluation fixtures were "dev", with no mechanism preventing reuse across splits.

## Decision

* The unit of splitting is a **family**: sources that share a template or restate the same facts, including the SQL question template that computes figures a document restates. Every source belongs to exactly one family, and every family has exactly one split.
* Anything that was used for an M1–M6 development fixture or decision is `dev` permanently. Held-out families are **sealed**: no item may be written from them before the release evaluation.
* The manifest is versioned and hash-locked (manifest and member files). It is never edited, only superseded by a new version.
* Derived records name their seed or legacy parent, must match its family and split, and must cite sources from one family only.
* A deterministic CI check rebuilds the pilot and rejects any split crossing, any change to a frozen manifest or source, any non-exact evidence, and any non-atomic claim.
* The pilot is labelled `pilot: true, release: false` and lives under `fixtures/evaluation/m7/pilot/`. The release evaluation will use a later manifest version over regenerated documents.
* Gold labels come only from two independent human annotators, with adjudication. An AI author's proposal is stored separately and is never gold.

## Consequences

The v1 held-out family is too small for a release result. This is intentional: honest exposure accounting leaves almost nothing unexposed in the M1–M6 corpus. Release numbers wait for manifest v2. Evidence spans are bound to the native parser's canonical text, so a parser or chunker change that alters that text fails the check and requires a deliberate rebuild.
