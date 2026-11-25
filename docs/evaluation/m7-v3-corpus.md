# M7 v3 corpus: a public benchmark with a held-out split (frozen, no derived data yet)

Status: **corpus prepared and frozen.** `split-manifest-v3` is the active manifest. No claim, question, checker pair, fine-tuning run or threshold exists for the fresh families. Those steps come later, after review of this freeze.

**What v3 is.** v3 is a **public benchmark with a held-out split**:
- Every v3 document, its facts and its generator are in this repository, readable by anyone with access, including whoever trains or tunes.
- Its held-out split guards against *accidental* contamination. ML code can only read data through the split-enforcing loader (section 6), and the CI audits fail on direct readers.
- It is **not** a blind test. A determined reader can open the held-out files, and the AI author who wrote the generator has seen them.
- Results on v3 held-out are therefore reported as *public-benchmark* results. The release claim rests on **blind release data**, which a coordinator keeps outside the training workspace (section 7).

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

## 6. Keeping held-out material out of training, tuning and model selection

**The split-enforcing loader** (`backend/app/evaluation/loader.py`) is the only supported way for ML code to read M7 sources:

```python
from app.evaluation import loader
train_docs = loader.load("training").documents        # train split only (60 documents)
dev_docs = loader.load("model-selection").documents    # dev split only (17 documents; the OCR scan is skipped)
```

| Purpose | Splits it may read |
| --- | --- |
| `training` | train |
| `tuning`, `model-selection` | dev |
| *(none)* | held-out: no purpose can read it |

How a call works:
1. It reads only the reviewed snapshot, the active manifest and its lock, and checks their hashes. It does **not** read `corpus-v3/corpus.toml`, which lists every title, held-out ones included.
2. It selects eligible references from the manifest's family-to-split table.
3. Only then does it open each eligible `<key>.md` and `<key>.json`, at paths built from the key. It never lists or globs a directory.
4. It verifies each loaded document and sidecar against the frozen lock before returning it.

Held-out files sitting in the same directory are therefore never opened.

**Regression tests** run the loader in a subprocess under a Python audit hook that records every `open` and directory listing:
- `training` opens only train documents and sidecars, 60 of each;
- `tuning` and `model-selection` open only dev sources, and none of `corpus-v3`;
- neither mode opens any of the 24 held-out documents or sidecars, the corpus index, or a directory listing, **including when all 84 documents and 84 sidecars share one directory**;
- the same hook, applied to a naive `glob('*.md')` reader, records all 24 held-out opens. So the tests do detect a leak when one exists.

**CI audits** (tripwires; the loader is the control):
- **Direct readers:** CI fails if any test, app module, script, `ml/` file, evaluation fixture or `docs/evidence` capture refers to `corpus-v3` / `corpus_v3` or `evaluation/m7` paths, or imports any `app.evaluation` module other than `loader`. A bulk reader therefore cannot load all 84 documents without tripping it, even though it never names a held-out file. Code that builds paths dynamically from fragments could evade the text scan, so reviews must also check that ML code reads data only through the loader.
- **Named members:** CI also fails if such an artifact names a train or held-out member.
- **Sealed:** no pilot or derived record may use a held-out family.

Only M7's own files may reference fresh members directly: `app/evaluation` (integrity checks hash and compare every member and never return content to ML code), `fixtures/evaluation/m7`, `tests/unit/test_m7_evaluation.py`, and `docs/evaluation` (not scanned).

**Disclosure:** an AI assistant wrote the generator and templates and therefore saw the held-out documents. Nobody has tuned or selected a model on them. Thresholds are selected on dev only.

## 7. Blind release data (coordinator procedure; not built yet)

The release evaluation needs data that nobody training, tuning or selecting has had any access to. One person, the **coordinator**, who does not train, tune or select models, will:

1. **Create it outside the training workspace:**
   - Generate new release families on the coordinator's own machine, or in a private repository that training contributors and their agents cannot read. Never in this repository, a shared drive or a CI artifact.
   - Use templates of their own, not this repository's generators: v3's templates are public, so data from them is not blind.
2. **Freeze it there** with the same discipline:
   - a manifest of release families (one template each, whole families in one split);
   - a lock with SHA-256 per document and per facts file;
   - the overlap check against this repository's dev, train and v3 held-out text.
3. **Publish only freeze metadata** to this repository, as a reviewed snapshot entry, for example `fixtures/evaluation/m7/blind-release-v1.json`, containing:
   - the manifest's and the lock's SHA-256;
   - the number of families and documents per split;
   - the generator version and the SHA-256 of the coordinator's generator source;
   - a **commitment** to the seed (`SHA-256(seed ‖ salt)`), never the seed itself;
   - the freeze date.

   It must contain no documents, facts, titles, keys, family names that reveal topics, seeds or generator code. CI will compare the entry with `main` like any snapshot entry, so it cannot be rewritten after the fact.
4. **Fix the system under test first.** Before scoring, the team freezes the checker and model artifacts and the dev-selected thresholds, and records their SHA-256 in the repository.
5. **Score in the coordinator's workspace.**
   - The coordinator runs the frozen artifacts on the blind data and publishes per-metric results.
   - The coordinator then reveals the data, generator, seed and salt. Anyone can check them against the committed hashes and commitment.
   - After the reveal, the blind set becomes a public benchmark like v3, and the next release needs new blind data.

Nothing in this procedure has been carried out yet. This PR only documents it.

## 7. Reproduction

```bash
cd backend
.venv/bin/python -m app.evaluation build-corpus-v3 && git status --short fixtures/evaluation/m7/corpus-v3   # must be empty
.venv/bin/python -m app.evaluation overlap            # JSON report; exit 1 on any overlap
git fetch origin main && .venv/bin/python -m app.evaluation check --base origin/main --git-history
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py
```

## 8. Gaps

**Update:** the first checker training batch (2,088 silver pairs from the train split) is in [m7-checker-batch-001.md](m7-checker-batch-001.md).

0. **Blind release data does not exist yet:** v3 held-out is a public benchmark split (section 7). A coordinator still has to be chosen, and must create, freeze and publish metadata for blind data before release scoring.
1. **No derived data for the fresh families yet:** QA cases, retrieval pairs, checker training pairs and gold pairs are the next, separately reviewed step.
2. **Scale.** 60 train documents (about 6–10 facts each) will not reach 3,000–5,000 checker pairs without many perturbations per fact. 24 held-out documents can support roughly 200–250 gold claims, below the 300–500 target. Reaching the targets needs a v4 manifest with more documents or families, frozen the same way. Growing v3 in place is not allowed.
3. **Format coverage.** Fresh documents are Markdown only: no PDF, scan, spreadsheet or CSV in train or held-out. Parser-dependent evaluation stays dev-only until those formats are added.
4. **Permissions.** Fresh documents have no ACL metadata yet. Retrieval and authorization evaluation on them needs permissions assigned before upload.
5. **Original generator:** the rebalanced NileTech baseline from the roadmap is still not produced. The original generator's document templates cannot supply fresh data (section 1).
6. **Arabic** is out of scope until the owner decides.
7. Branch protection on `main` (a required check) underpins the base-branch anchor.
