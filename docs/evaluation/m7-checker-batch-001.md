# M7 checker training pairs: batch 001 (silver, train split only)

Status: **first reproducible batch generated, revised after review.** No model was trained and no threshold was selected. Labels are **silver**: generated from the documents' facts sidecars and not reviewed by a person.

## Revision 2 (PR #12 review)

1. **Ambiguous silver labels corrected.**
   - **Decision claims** now state an exact status: `records the decision status for S as exactly "approved"`. The 3 pairs that marked "approved" unsupported against "approved with conditions" are now unambiguous status mismatches.
   - **Start-year claims** now state the start year: `NileTech started using S in YYYY`. Later-year variants (13 after rebuilding) are now unambiguously wrong; the old "used since a later year" reading is gone.
   - **Same audit, three more claim types:** "allows N labour hours", "affected N client accounts" and "lasted N minutes" could be read as lower bounds, so they now say "in total".
   - **Entailment guard:** a general `still_entailed` rule drops any unsupported variant that its own evidence still entails. Only equal values are entailed, except upper-bound claims ("within N hours"), where any looser bound is entailed. Every pair now records `claim_value` and `evidence_value`, and `validate` rejects an unsupported pair whose evidence still entails it.
2. **Batch 001 is pinned to the frozen v3 manifest.**
   - `checker_pairs.PIN` names `m7-split-v3` with its reviewed manifest SHA-256 (`be2205db…`) and lock SHA-256 (`39456f46…`).
   - `loader.load(..., pin=PIN)` loads exactly that snapshot, even when a newer manifest is active, after checking the snapshot entry against the pin.
   - Build, render and check all use the pin, and `index.json` records both hashes.
   - A test activates a newer v4 manifest (which moves a train family to held-out) and shows two things: the unpinned training load follows v4 (40 documents), while batch 001 still rebuilds byte for byte from the pinned v3 (60 documents).

## How it is built

`backend/app/evaluation/checker_pairs.py` (generator version `1.0.0`, batch seed `20261127`) reads its input **only** through the approved v3 loader in training mode, pinned to the frozen v3 manifest: `loader.load("training", pin=PIN)`. That call opens only the 60 train documents and their facts sidecars, after selecting them from the frozen `m7-split-v3` metadata.

* **Atomic claims.** Each claim is one sentence stating one fact of one document, built from 28 claim types: 11 for work orders, 9 for supplier reviews, 8 for incident reviews.
* **Exact evidence.** Each pair carries:
  - the single sentence of the source's canonical text that states the fact, with its character offsets;
  - the document title, the evidence document's ref, and a SHA-256 of its canonical text;
  - the exact checker input as `premise` (the title plus the evidence sentence, or the title alone when the evidence is the title line).
* **Traceability.** Each pair records `family`, `split`, `source`, `fact`, `claim_type`, `parent` (for variants), `label_quality: silver` and `label_source`.
* **Unsupported variants are plausible.** Every variant keeps its parent's document, family and split:

| Variant | Meaning | Pairs |
| --- | --- | ---: |
| `original` | the supported claim | 560 |
| `swap` | the same fact's value from another document of the same family | 535 |
| `shift` | a nearby number, amount, year or date | 306 |
| `distractor` | another value of the same document (estimate vs final cost; raised vs closed; began vs action due) | 110 |
| `polarity` | the negated yes/no statement (personal data) | 20 |
| `wrong_evidence` | the original claim against the same fact's sentence in another document of the same family | 560 |

  A claim that states a bound ("requires a response within N hours") is only perturbed to a *stricter* value, because a looser bound would still be entailed.
* **Deduplication.** Exact duplicates (same claim, title and evidence) are removed and counted, and identical pairs with different labels are reported as conflicts.

## Actual counts

| Family (train) | Supported | Unsupported | Total |
| --- | ---: | ---: | ---: |
| `fresh-train-regenerated` (work orders) | 220 | 644 | 864 |
| `fresh-train-supplier-due-diligence` | 180 | 439 | 619 |
| `fresh-train-incident-review` | 160 | 448 | 608 |
| **All** | **560** | **1,531** | **2,091** |

* 2,113 pairs generated; **22 exact duplicates removed**; **0 label conflicts**; 0 unsupported pairs still entailed by their evidence.
* Source documents: 60 train. Dev and held-out pairs: 0.
* Label balance: 26.8% supported. A training run will need class weighting or sampling; that is not decided here.

## Token lengths (pinned DeBERTa tokenizer)

Measured with `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` at revision `6f5cf0a2b59cabb106aca4c287eed12e357e90eb`:
- Tokenizer class `DebertaV2TokenizerFast`, `transformers==4.56.2`.
- The six tokenizer files are pinned by SHA-256 in `app/evaluation/token_lengths.py`. No model weights are downloaded.
- The counting rule is the gateway's own: `len(tokenizer(premise, claim, truncation=False)["input_ids"])`, special tokens included, against `CHECKER_MAX_TOKENS = 512`.

| | Pairs | Min | Median | p95 | Max | Over 512 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 2,091 | 24 | 46 | 59 | 64 | **0** |
| work orders | 864 | 37 | 54 | 61 | 64 | 0 |
| supplier reviews | 619 | 32 | 45 | 53 | 54 | 0 |
| incident reviews | 608 | 24 | 39 | 49 | 54 | 0 |

The per-pair counts are committed in `checker/batch-001/token-lengths.json`. The CI job `checker-tokenizer` installs `ml/checker-tokenizer/requirements.txt` with `--no-deps` (exact pins), recounts every pair, and fails on any difference or any pair over 512 tokens. The backend check also verifies that the report covers exactly the committed pairs and the pinned tokenizer.

## Split isolation (tests)

* An audit-hook probe records every file opened and every directory listed while the batch is built. The build opens no held-out document or sidecar, no dev document (M3 corpus, contracts, code fixtures), no corpus index, and lists no directory.
* `build_pairs` refuses a non-training load, and refuses a dev or held-out document smuggled into a training result.
* `validate` rejects any pair whose source or evidence is a dev or held-out document. It also rejects:
  - a variant that leaves its parent's family, split or source;
  - wrong-evidence from another family;
  - evidence that is not the exact span at its offsets, or a premise that is not title + evidence;
  - non-atomic claims, a supported variant, or a non-silver label.
* No train claim shares normalised text with any dev pilot record.
* The committed batch must equal a fresh build (determinism), checked in `python -m app.evaluation check`.

## Gap to the roadmap target

The target is 3,000–5,000 checker training pairs. Batch 001 has **2,091: 909 short of the lower bound**, and 2,909 short of the upper bound. Options for the next batch:
- more train documents via a new manifest version, frozen the same way (v3 cannot grow in place);
- more claim types per document;
- multi-sentence and paraphrased claims.

Each needs its own review.

Other gaps:
- Labels are silver: a sample needs human review before the labels are trusted.
- No neutral vs contradiction distinction (binary only).
- Variants are template-based: claim wording repeats within a claim type, so the model may learn surface patterns. Paraphrase diversity is a known limitation.
- English only; Markdown sources only.
- Stopped before training and threshold selection, as instructed.

## Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation build-checker-batch && git status --short fixtures/evaluation/m7/checker   # empty
.venv/bin/python -m app.evaluation check
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py

# Token lengths, in an isolated environment (no torch); fetches only the pinned tokenizer files.
cd ..
python3.13 -m venv /tmp/tok-env && /tmp/tok-env/bin/pip install --no-deps -r ml/checker-tokenizer/requirements.txt
/tmp/tok-env/bin/python -I backend/app/evaluation/token_lengths.py \
  --pairs backend/fixtures/evaluation/m7/checker/batch-001/pairs.jsonl \
  --tokenizer-dir /tmp/deberta-tokenizer \
  --check backend/fixtures/evaluation/m7/checker/batch-001/token-lengths.json
```
