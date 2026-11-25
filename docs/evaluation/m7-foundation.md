# M7 evaluation foundation (pilot)

Status: **foundation and pilot only.** No model was fine-tuned, no threshold was chosen and no human labels exist yet. Every dataset is a clearly labelled **pilot**, kept separate from the release evaluation. The roadmap's dataset sizes are targets.

Revision 2 (2026-11-26) addresses the four PR #10 review findings: blind annotation, evidence and provenance validation, the corrected split inventory, and an enforceable freeze.

**Update (v3):** `split-manifest-v3` is now active. It keeps the nine dev families below unchanged and adds fresh train and held-out families from a new deterministic generator. See [m7-v3-corpus.md](m7-v3-corpus.md). The pilot below is unchanged; only its index now points at v3.

## 1. Inventory of what existed before M7

| Asset | Location | What it is | Reuse in M7 |
| --- | --- | --- | --- |
| Frozen M1 SQL cases | `backend/fixtures/evaluation/sales-q1-2026.toml` | 2 cases (Q1 2026 net fulfilled sales; isolation-tenant abstention) with independently computed values. `split = "dev"`. | Dev legacy fixture of family `q1-2026-sales-figures`; its values are cited by pilot QA claims. |
| M2 generalization SQL cases | `backend/fixtures/evaluation/sales-generalization-m2.toml` | 3 more periods of the same question template. Dev. | Same family/template (`sql:sales-period-net-fulfilled`). |
| M3 retrieval dev cases | `backend/fixtures/evaluation/documents-dev-m3.toml` | 22 query → document + exact substring. Dev. | 22 pilot retrieval pairs (`ret-m3-*`), parent `legacy:<path>#<id>`. |
| M3 relevance study | `backend/fixtures/evaluation/relevance-study-m3.toml` | 24 hand-labelled question/passage pairs. Dev. | 24 pilot retrieval pairs (`ret-m3rel-*`), 12 of them negatives. |
| Retrieval harness | `backend/app/documents/evaluate.py`, `app.cli retrieval-eval` | Production parser → chunker → embeddings → temporary Qdrant collection. Compares dense with hybrid on recall@1/5 and MRR@5. | Reusable for metrics. **Gap:** its case file is hard-coded and it scores positives only. |
| Development checker | `ml/model-gateway/gateway.py` (`/check`), `app/reporting/model_client.py`, `app/config.py` | Public `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli`, pinned revision; `CHECKER_ENTAILMENT_THRESHOLD = 0.5` (uncalibrated). | The baseline for the later fine-tuned checker. Unchanged. |
| Deterministic claim checks | `app/reporting/claims.py`, M5 citation verifier | Exact-number and citation checks. | The pilot uses the same "exact value in cited evidence" rule. |

**Missing data (still missing):**
- No source is free of development use: every current document, contract and code fixture was used by M1–M6 tests, runs or evidence (section 2).
- There is no train data and no release-grade held-out data.
- No checker training data at the planned scale exists.
- No human-labelled checker gold exists.

## 2. Split manifests: v1 (superseded audit record) and v2 (active)

* **The family is the unit of splitting.** A family groups sources that share a template or restate the same facts. Every document, contract, code fixture and SQL question template belongs to exactly one family.
* **v1** (`split-manifest-v1.toml`, frozen in `57ed79c`) is kept **byte-identical** as an audit record. Its lock must still verify. The review showed that v1 misplaced three families:
  - `code-config-constants` → train. In fact it was used by the M5 GitHub acceptance runs, including the real-model Colab run.
  - `board-governance` → held-out. In fact it was indexed in the M3 Colab retrieval evaluation that chose dense retrieval, and used by M3 answer tests.
  - `contract-template` → train. The re-audit found it drove the M5 legal extractor tests.
* **v2** (`split-manifest-v2.toml`, frozen in `ba6e9c8` **before** the pilot was regenerated) puts all **9 families in dev**. Each family lists its `exposure` evidence.
  - It reserves fresh families with no members yet: `fresh-train-regenerated` (train), and `fresh-heldout-regenerated-a` and `-b` (held-out, sealed).
  - Their members will be regenerated NileTech documents, added in a v3 manifest that is frozen before any claim is written.
* **Automatic exposure audit.** The check scans `backend/tests`, `backend/app`, `backend/fixtures/evaluation`, `scripts`, `ml` and `docs/evidence` (excluding M7's own files) for every member's key, file name or path.
  - Any hit for a train or held-out family fails CI. A future test that touches a held-out document is therefore caught.
  - Every listed exposure path must exist and mention a member.
  - Run against v1, the audit flags exactly the three misassigned families. A regression test keeps it that way.

## 3. Enforceable freeze

Regenerating a lock and the pilot after editing a manifest must not pass. Three anchors that tools cannot rewrite:

1. **Reviewed snapshot**: `backend/fixtures/evaluation/m7/frozen-snapshots.json`.
   - Written by hand, never by a tool. `freeze` only *prints* the entry.
   - Pins every manifest and lock by SHA-256, with status `active` or `superseded`.
   - The check verifies every registered manifest (v1 too) against the snapshot and against its own lock.
2. **Base branch.** CI runs `check --base origin/main`.
   - Every manifest, lock and snapshot entry that already exists on `main` must be unchanged. The only allowed transition is `active` → `superseded`.
   - Rewriting a manifest, its lock and its snapshot hash together still fails once the snapshot is on `main`.
   - Before merge, the snapshot is what the reviewer checks in this PR.
3. **Freeze before derive.** CI runs `check --git-history`. The commit that added the active lock must be a strict ancestor of the latest commit that changed `pilot/datasets.json`.

`test_changed_manifest_with_regenerated_lock_and_pilot_still_fails` edits v2, regenerates its lock and the whole pilot consistently, and shows that the check still fails, and only on the freeze. `test_rewriting_manifest_lock_and_snapshot_together_fails_against_base` does the same and also rewrites the snapshot, in a real git repository; the base-branch comparison catches it.

## 4. Evidence and provenance validation

For every pilot record the check:
- gathers **every source actually used**: `source`, `evidence_source`, `claim_about`, `allowed_sources`, and each QA claim's `source`;
- rejects any source not declared in the record's `sources`, any source not in the manifest, and any record whose sources span several families;
- requires the record's split to equal its family's split;
- resolves `derived_from` against the manifest.
  - `seed:<id>` must name a seed in `seeds.toml`, and the family is computed from the seed's own sources.
  - `legacy:<path>#<id>` must name a manifest legacy fixture and an item that exists in it.
  - Unknown parents fail. A parent in another family or split fails.

Regression tests use a test-only copy of v2 with the code family moved to train, because no train family exists in v2:
- a dev claim with undeclared train evidence fails;
- a dev claim with the train evidence declared still fails (two families);
- a train record whose parent is a real dev seed (`seed:gold-hr-egypt-days`) fails;
- a train record whose parent is a real dev legacy item (`legacy:…documents-dev-m3.toml#fin-net-q1`) fails;
- four kinds of unknown parent fail.

## 5. Pilot datasets (actual counts under v2)

Source: `pilot/seeds.toml` plus the frozen pre-M7 fixtures. Regenerate with `python -m app.evaluation build-pilot`. Every record has `pilot: true` and `release: false`.

| Dataset | Count | train | dev | held-out | In intended split | Roadmap target | Label source |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| Question–answer cases | 12 (14 atomic claims, 2 abstentions) | 0 | 12 | 0 | 0 (held-out) | 100–200 | AI author draft; SQL values come from frozen fixtures |
| Retrieval query–chunk pairs | 57 (42 relevant, 15 not) | 0 | 57 | 0 | 0 (held-out) | 200–300 | 46 frozen M3 fixtures (reviewer not recorded), 11 AI drafts |
| Checker training pairs | 78 (24 supported, 54 unsupported) | 0 | 78 | 0 | **0 (train)** | 3,000–5,000 | Silver; a **format exercise only**, because dev families must not train a model whose thresholds are chosen on dev |
| Gold checker pairs | 37 (proposed 18 / 19) | 0 | 37 | 0 | 0 (held-out) | 300–500, double-labelled | **No human labels** |

Changes from revision 1: all 11 former train retrieval pairs and all 78 former train checker pairs are now dev. **No record is usable for training or for held-out evaluation.**

**Human review actually received: 0 of 37 gold pairs, and 0 of every other record.** Cohen's kappa is n/a.

Claims are atomic: one sentence, no semicolons, at most 30 words. Evidence is an exact span of the app's canonical parser text, or an exact frozen SQL fixture value.

## 6. Blind two-annotator labelling

See [m7-labelling.md](m7-labelling.md). In short:
- A coordinator exports a blind sheet per annotator. Sheets carry opaque random item ids, their own order, no pair id, no perturbation type and no proposed label.
- The id-to-pair key stays with the coordinator (`var/`, git-ignored).
- `labels import` needs **both** filled sheets with their keys in one call and maps the ids back to pairs.
  - It requires a valid label on every item of both sheets.
  - It rejects any change to the claim, evidence title or evidence, any sheet or key from another dataset revision, edited or deleted items, foreign rows, invalid labels and re-imports.
  - It publishes both files or neither: staged writes with rollback, so a failed import can be retried cleanly.
- `labels status` reports per-pair states, disagreements, Cohen's kappa, adjudications and the ids of human-reviewed pairs.

## 7. Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation check                                   # local
git fetch origin main && .venv/bin/python -m app.evaluation check --base origin/main --git-history   # as CI
.venv/bin/python -m app.evaluation build-pilot && git status --short fixtures/evaluation/m7          # must be empty
.venv/bin/python -m app.evaluation report --json
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py
```

## 8. Remaining gaps (before the release evaluation)

1. **Fresh data:** generate the NileTech documents for the reserved families. Freeze them as v3 (reviewed snapshot entry included) before writing claims. Nothing usable for train or held-out exists yet.
2. **Human labels:** two annotators label the 37 pilot gold pairs through the blind workflow to measure kappa. The real 300–500 gold pairs come from the held-out families.
3. **Scale:** every dataset is roughly 3.5–40× below the lower bound of its target.
4. **Train-split QA and further SQL templates** (only one SQL template exists, and it is dev).
5. **Retrieval harness:** read `retrieval-pairs.jsonl`, including negatives, contracts and code.
6. **Checker token budget:** count pair lengths with the real DeBERTa tokenizer (Colab). Define neutral vs contradicted and a multi-passage evidence policy.
7. **Arabic scope** decision.
8. **Branch protection:** the base-branch anchor is only as strong as the rule that `main` changes through reviewed PRs. Enable branch protection and require the `backend` check.
9. **Out of scope by instruction:** human labelling itself, fine-tuning, calibration, dev-only thresholds, false-support rate, latency, baselines.
