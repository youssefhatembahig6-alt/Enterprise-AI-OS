# ADR 0701: Family-level frozen evaluation splits and a separate pilot

Status: Proposed for M7 (pending planning review)

## Context

The roadmap requires four distinct datasets and a frozen document/template-family split before derived claims are generated, so that the same template cannot contaminate train and held-out sets. Before M7 all evaluation fixtures were "dev", with no mechanism preventing reuse across splits.

## Decision

* The unit of splitting is a **family**: sources that share a template or restate the same facts, including the SQL question template that computes figures a document restates. Every source belongs to exactly one family, and every family has exactly one split.
* Anything mentioned by an M1–M6 test, script, ML run, evidence capture or evaluation fixture is development-exposed and `dev` permanently. An automatic scan enforces this. Train and held-out data come only from **fresh** families, which are reserved without members until their documents exist. Held-out families are **sealed**: no item may be written from them before the release evaluation.
* The manifest is versioned and hash-locked (manifest and member files). It is never edited, only superseded by a new version; superseded versions stay as audit records. The freeze is anchored outside the tools: a hand-written reviewed snapshot (`frozen-snapshots.json`) pins every manifest and lock by SHA-256; CI compares frozen files and snapshot entries with `main`; and the freeze commit must precede the pilot commit.
* Every evidence source a record uses must be declared and must be in the manifest. A record cites sources from one family only. Its seed or legacy parent must resolve to the same family and split.
* A deterministic CI check rebuilds the pilot and rejects any split crossing, any change to a frozen manifest or source, any non-exact evidence, and any non-atomic claim.
* The pilot is labelled `pilot: true, release: false` and lives under `fixtures/evaluation/m7/pilot/`. The release evaluation will use a later manifest version over regenerated documents.
* Gold labels come only from two independent human annotators, with adjudication. Annotators get blind sheets with opaque ids, and both submissions are imported together. An AI author's proposal is stored separately and is never gold.

## Consequences

Revision 2 (after the PR #10 review): v1 misplaced three development-exposed families in train and held-out. v2 puts all nine current families in dev, so the pilot has no train or held-out records. Release numbers wait for a v3 manifest over regenerated documents. The base-branch anchor relies on `main` being protected (reviewed PRs, required CI). Evidence spans are bound to the native parser's canonical text, so a parser or chunker change that alters that text fails the check and requires a deliberate rebuild.
