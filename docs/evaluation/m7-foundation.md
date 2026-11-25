# M7 evaluation foundation (pilot)

Status: **foundation and pilot only.** No model was fine-tuned and no threshold was selected. Every dataset here is a small, clearly labelled **pilot**; none of it is the release evaluation, and the roadmap's dataset sizes remain targets.

## 1. Inventory of what existed before M7

| Asset | Location | What it is | Reuse in M7 |
| --- | --- | --- | --- |
| Frozen M1 SQL cases | `backend/fixtures/evaluation/sales-q1-2026.toml` | 2 cases (Q1 2026 net fulfilled sales; isolation-tenant abstention) with independently computed values, distractors and a 10-order worked subset. `split = "dev"`. | Registered as a dev legacy fixture of family `q1-2026-sales-figures`. Values cited by pilot QA claims. |
| M2 generalization SQL cases | `backend/fixtures/evaluation/sales-generalization-m2.toml` | 3 more periods of the same question template. `split = "dev"`. | Same family and template (`sql:sales-period-net-fulfilled`). |
| M3 retrieval dev cases | `backend/fixtures/evaluation/documents-dev-m3.toml` | 22 query → document + exact expected substring. Dev split. | Converted into 22 pilot retrieval pairs (`ret-m3-*`). |
| M3 relevance study | `backend/fixtures/evaluation/relevance-study-m3.toml` | 24 hand-labelled question/passage pairs (paraphrase, trap, match, unrelated). | Converted into 24 pilot retrieval pairs (`ret-m3rel-*`), including 12 negatives. |
| Retrieval harness | `backend/app/documents/evaluate.py`, `python -m app.cli retrieval-eval`, `tests/integration/test_retrieval_eval.py` | Production parser → chunker → embeddings → temporary Qdrant collection; dense vs hybrid (RRF); recall@1/5, MRR@5, document recall@5; labels fake runs. Real BGE-M3 result recorded in `docs/evidence/m3/`. | Reusable as is for metrics. **Gap:** its case file path is hard-coded to `documents-dev-m3.toml` and it only scores positive cases; it must be parameterized to read `retrieval-pairs.jsonl` (and to use negatives) before M7 retrieval runs. |
| Development checker | `ml/model-gateway/gateway.py` (`POST /check`, `NliChecker`), `backend/app/reporting/model_client.py` (`check`), `backend/app/config.py` | Public `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` at a pinned revision, refuses inputs over its token limit; `CHECKER_ENTAILMENT_THRESHOLD = 0.5`, documented as uncalibrated. | The baseline the fine-tuned checker will be compared to. Untouched here. |
| Deterministic claim checks | `backend/app/reporting/claims.py`, M5 citation verifier | Exact-number and citation checks for SQL report sentences and legal values. | Complements the learned checker; the pilot QA claims use the same "exact value in the cited evidence" rule. |
| GPU timing workload | `ml/gpu-feasibility` (`m1-gpu-workload-v1`) | 10 questions for latency only; explicitly not an evaluation set. | Not used as evaluation data. |
| Parser gold | `ml/parser-benchmark/fixtures/gold` | Gold text/tables/reading order for 7 files. | Not evaluation data for QA; the newsletter/overview are dev-exposed through it. |

### What data was missing (and still is)

* **No split manifest existed.** Every pre-M7 file was labelled "dev", and nothing prevented the same document family appearing in a later train or test set. M7 adds it (section 2).
* **No held-out data of release quality.** Of the 16 source files, all but two were already used for development. The remaining family (board minutes + draft strategy) is tiny and is the only held-out family in v1. The release held-out set needs the regenerated NileTech documents (roadmap section 10).
* **No checker training data and no human-labelled checker gold.** Nothing existed. The pilot adds 78 silver training pairs and 37 gold candidates. None has a human label.
* **QA cases** had numeric expectations but no atomic claims with cited evidence spans.
* **Retrieval pairs** existed only as "the expected substring appears in top-k". They had no graded negatives outside the relevance study and no pairs for contracts or code.

## 2. Frozen split manifest

* `backend/fixtures/evaluation/m7/split-manifest-v1.toml` assigns **every** corpus document, contract, code fixture and SQL question template to exactly one family. Each family has exactly one split and a recorded reason.
* Families group sources that share a **template** or restate the **same facts**. For example, the Q1 commentary, Q1 sales tables, Q1 workbook, a Q1 delivery note, the isolation tenant's copy of the commentary's revenue sentence, and the SQL template that computes those figures form one family.
* Rule: anything used by an M1–M6 development fixture or decision is `dev` and can never become held-out. Unexposed multi-member template families are `train`. The remaining family is `heldout` and **sealed**: no pilot record may use it.
* Result: 6 dev families, 2 train families (`contract-template`, `code-config-constants`), and 1 held-out family (`board-governance`).
* `split-manifest-v1.lock.json` records the SHA-256 of the manifest and of every member source. The manifest and lock were committed in their own commit (`57ed79c`) **before** any seed claim or derived dataset existed. Changing the manifest or any member file fails the check. A change needs `split-manifest-v2`.
* Disclosure: while taking the inventory, the author (an AI assistant) read the full text of every fixture file, including the two held-out documents. No question, claim or pair was written from them. This exposure is a further reason that v1 held-out is not release-grade.

## 3. Pilot datasets

Source: `backend/fixtures/evaluation/m7/pilot/seeds.toml` (authored after the freeze) plus the frozen pre-M7 fixtures. Generated by `python -m app.evaluation build-pilot` into `pilot/generated/*.jsonl`, with `pilot/datasets.json` as the index (manifest SHA, seed SHA, per-file SHA, counts). Every record carries `pilot: true`, `release: false`, its family, split, sources and `derived_from`.

| Dataset | Pilot count | train | dev | held-out | Roadmap target | Label source |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| Question–answer cases | 12 (14 atomic claims; 2 abstentions) | 0 | 12 | 0 | 100–200 | AI author draft, not human-reviewed. SQL claim values come from frozen, independently computed fixtures. |
| Retrieval query–chunk pairs | 57 (42 relevant, 15 not) | 11 | 46 | 0 | 200–300 | 46 from frozen M3 fixtures (reviewer not recorded); 11 AI author drafts |
| Checker training pairs | 78 (24 supported, 54 unsupported) | 78 | 0 | 0 | 3,000–5,000 | Silver: authored seed claim, authored perturbation (number, unit, entity, scope, polarity) or automatic wrong-evidence pairing |
| Gold checker pairs | 37 (18 proposed supported, 19 proposed unsupported) | 0 | 37 | 0 | 300–500, each double-labelled | **No human labels yet.** The AI author's `proposed_label` is stored but never shown on sheets and never counted. |

**Human review actually received: 0 of 37 gold pairs; 0 of all other pilot records.** Cohen's kappa is therefore not yet computable. `python -m app.evaluation report` prints the live status.

Claims are atomic: one sentence, no semicolons, at most 30 words. Each claim is backed by an evidence span that is an exact substring of the application's own canonical text for that source, or for SQL claims by a frozen fixture value that the claim must state exactly. A checker pair's evidence is one span plus the source title (`evidence_title`). The title is needed because contract clauses say "this Agreement". Checker inputs must include both.

## 4. Two-annotator labelling, kappa and adjudication

See [m7-labelling.md](m7-labelling.md). In short: blind per-annotator sheets (own item order, no proposal shown) → `labels/gold-<annotator>.csv` → `labels status` reports per-pair state (unlabelled / single-label / agreed / disagreement-pending / adjudicated), the disagreement list, Cohen's kappa over double-labelled pairs, and the list of pairs that received any human review. Only agreed or adjudicated pairs count as gold.

## 5. Deterministic CI smoke check

`python -m app.evaluation check` (CI step "M7 evaluation smoke"; no network, database or model) fails if:

* the manifest is malformed, a source is unassigned or in two families, or the manifest or a member source changed after freezing;
* a pre-M7 fixture item or a pilot record is outside its family's split, cites several families, uses the sealed held-out family, or is derived from a seed in another family/split;
* the same question/claim/query text (case and digits normalised) occurs in two splits;
* an evidence span is not exact, an SQL claim does not state its fixture value, or a claim is not atomic;
* the generated files differ from a fresh rebuild, or the index does not point at the frozen manifest;
* a label or adjudication file is malformed.

`backend/tests/unit/test_m7_evaluation.py` mutates copies to prove each failure is detected, checks kappa against a textbook value, and checks the full two-annotator workflow.

## 6. Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation check          # the CI smoke check
.venv/bin/python -m app.evaluation build-pilot    # regenerate; `git diff` must be empty
.venv/bin/python -m app.evaluation report --json  # counts and label status
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py
```

## 7. Remaining gaps (before the release evaluation)

1. **Release held-out split:** regenerate NileTech documents into new families. Freeze them as `split-manifest-v2` (held-out families sealed, nobody tuning on them reads them) before writing claims.
2. **Scale:** every dataset is roughly 3.5–40× below the lower bound of its roadmap target (table above).
3. **Human labels:** two independent annotators must label the 37 pilot gold pairs to exercise the workflow and measure kappa. Then the real 300–500 gold pairs come from the release families.
4. **Train-split QA and SQL templates:** no train-split QA exists. Only one SQL question template exists, and it is dev.
5. **Retrieval harness:** parameterize `run_retrieval_eval` to read `retrieval-pairs.jsonl`, including negatives and contracts/code.
6. **Checker token budget:** pair lengths are not yet counted with the DeBERTa tokenizer (the roadmap requires counting with each actual tokenizer). Do this in the Colab gateway environment.
7. **Label scheme:** binary supported/unsupported. A neutral vs contradicted split and a multi-passage evidence policy are not defined. Compound claims are excluded rather than tested.
8. **Language:** English only until the owner confirms Arabic scope (two Arabic retrieval cases are inherited from M3).
9. **Out of scope here by instruction:** fine-tuning, calibration, threshold choice (dev only, later), false-support rate, latency, and baseline comparisons.
