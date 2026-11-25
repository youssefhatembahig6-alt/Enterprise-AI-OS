# M7 v3 corpus: fresh train and held-out families (frozen, no derived data yet)

Status: **corpus prepared and frozen.** `split-manifest-v3` is the active manifest. No claim, question, checker pair, fine-tuning run or threshold exists for the fresh families. Those steps come later, after review of this freeze.

## 1. The original NileTech generator

| Item | Finding |
| --- | --- |
| Where | Old repository `youssefhatembahig6-alt/enterprise-ai-oshh`, commit `bf4a247a5c3db8bcedc37e0ed47dad6c635d9349` (version 0.1.6, seed 20260630), `scripts/seed/src/eaios_seed` (`generators/{legal,policies,public,hr,sales}.py`, `docgen.py`). Its export is `../reference-data/niletech-0.1.6.zip` (70 documents). |
| Used for v3? | **No.** |
| Why not | 1. The dev corpus came from its templates. Its public-services text appears verbatim in dev documents (e.g. "Replace manual back-office steps with governed workflows."), and its stated policy values (14-character passwords, 5 carry-over days, 21 leave days in Egypt) are restated by the dev HR and IT documents. New documents from it would share templates and facts with dev families. 2. It depends on the old `eaios_core` package and on the removed second tenant, so it does not run in this repository. |
| Gap | The original generator's document templates cannot supply fresh data. The rebalanced 70-document NileTech baseline that the roadmap (M1 details) describes has not been produced. v3 uses a new, purpose-built generator. |

## 2. The v3 generator

`backend/app/evaluation/corpus_v3.py`, name `eaios-m7-fresh-corpus`, version `3.0.0`.

* **Seed** `20261126`. Each family gets its own `random.Random`, seeded from SHA-256(`seed:family`). Dates are offsets from the fixed reference date `2026-06-30`. There are no clocks, UUIDs or unordered iteration, and output is byte-identical across runs (checked in CI).
* **Recorded provenance** in `backend/fixtures/evaluation/m7/corpus-v3/corpus.toml`: generator name, version, source path and source SHA-256; seed; reference date; the original generator's location and why it was not used; and, per document, family, split, template, title, file, facts sidecar, and the SHA-256 of both.
* **Facts sidecars** (`corpus-v3/facts/<key>.json`) hold each document's ground truth. A test checks that every fact is stated in its document. Later claim and pair generation will use them. No claim is written here.
* The generator source SHA-256 changed once after the freeze commit, because of a lint-only fix (a `noqa` comment, quote style and line wrapping). The generated documents and facts did not change: the v3 lock, which hashes every document with its facts, still verifies. `corpus.toml` records the current source hash.

## 3. Families and splits (active manifest `m7-split-v3`)

| Family | Split | Template | Documents |
| --- | --- | --- | ---: |
| 9 development-exposed families, unchanged from v2 | dev | (as in v2) | 18 source files + 1 SQL template |
| `fresh-train-regenerated` | train | facility work order | 20 |
| `fresh-train-supplier-due-diligence` | train | supplier due-diligence review | 20 |
| `fresh-train-incident-review` | train | post-incident review | 20 |
| `fresh-heldout-regenerated-a` | **held-out (sealed)** | client project status report | 12 |
| `fresh-heldout-regenerated-b` | **held-out (sealed)** | purchase requisition with quotes | 12 |

Totals: 14 families. 102 source files: 18 dev, 60 train, 24 held-out. Each fresh family has exactly one template and its own name pools and fact ranges, and whole families are assigned to splits.

## 4. Overlap checks (actual results)

`app/evaluation/overlap.py`, run by `python -m app.evaluation overlap` and inside `check`:

* **Template overlap:** shared word 8-grams after lower-casing and masking digits, so documents from the same template collide even when their numbers differ.
* **Fact overlap:** every proper name in a fresh family's facts (sites, vendors, suppliers, clients, services, items, cities, awarded suppliers) must be absent from dev text and from other fresh families.

| Compared against | Result |
| --- | --- |
| 22 dev texts (canonical text of every dev member, the 4 pre-M7 evaluation fixtures, pilot seeds) | 0 shared 8-grams, 0 shared names, for all 5 fresh families |
| 70 documents of the original generator's archive (local only, skipped in CI) | 0 shared 8-grams |
| Each pair of fresh families (10 pairs, including all 6 train/held-out pairs) | 0 shared 8-grams, 0 shared names |

Regression tests show that the check catches dev text copied into a fresh family, a reused fact name, and a template shared across a train/held-out pair.

## 5. Freeze order

| Commit | Contents |
| --- | --- |
| `96d5789` | Generator, overlap checker, 84 documents and facts (no split assignment) |
| **`497632c`** | **Standalone freeze:** `split-manifest-v3.toml`, its lock, and the hand-written snapshot entry (v2 `active` → `superseded`, v3 `active`). No code, no derived data. |
| later commits | CI wiring, tests, docs, the generator lint fix, and the pilot **re-index** under v3 (`datasets.json` manifest hash, `seeds.toml` manifest name). Pilot records are byte-identical, and no claim or question was added. |

CI enforces the order: `check --git-history` requires the active lock's commit to precede the pilot-index commit. `check --base origin/main` requires v1, v2 and their snapshot entries to be unchanged from `main`; only v2's `active` → `superseded` is allowed.

## 6. Keeping held-out material out of tuning and model selection

* Held-out families are **sealed**: no pilot or derived record may use them (`check_records`, tested with a fresh held-out document).
* The **exposure scan** fails CI if any test, app module, script, `ml/` file, evaluation fixture or `docs/evidence` capture mentions a train or held-out member (tested with a simulated threshold-tuning script that loads a held-out document).
* Only M7's own files may reference them: `app/evaluation`, `fixtures/evaluation/m7`, `tests/unit/test_m7_evaluation.py`, and `docs/evaluation` (which is not scanned).
* **Disclosure:** an AI assistant wrote the generator and its templates, and therefore saw the held-out documents. Nobody has tuned or selected a model on them. Rule for later work: thresholds are selected on dev only, and the held-out families are read once, for the final release run.

## 7. Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation build-corpus-v3 && git status --short fixtures/evaluation/m7/corpus-v3   # must be empty
.venv/bin/python -m app.evaluation overlap            # JSON report; exit 1 on any overlap
git fetch origin main && .venv/bin/python -m app.evaluation check --base origin/main --git-history
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py
```

## 8. Gaps

1. **No derived data for the fresh families yet:** QA cases, retrieval pairs, checker training pairs and gold pairs are the next, separately reviewed step.
2. **Scale.** 60 train documents (about 6–10 facts each) will not reach 3,000–5,000 checker pairs without many perturbations per fact. 24 held-out documents can support roughly 200–250 gold claims, below the 300–500 target. Reaching the targets needs a v4 manifest with more documents or families, frozen the same way. Growing v3 in place is not allowed.
3. **Format coverage.** Fresh documents are Markdown only: no PDF, scan, spreadsheet or CSV in train or held-out. Parser-dependent evaluation stays dev-only until those formats are added.
4. **Permissions.** Fresh documents have no ACL metadata yet. Retrieval and authorization evaluation on them needs permissions assigned before upload.
5. **Original generator:** the rebalanced NileTech baseline from the roadmap is still not produced. The original generator's document templates cannot supply fresh data (section 1).
6. **Arabic** is out of scope until the owner decides.
7. Branch protection on `main` (a required check) underpins the base-branch anchor.
