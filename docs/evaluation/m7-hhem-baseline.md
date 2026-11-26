# M7 baseline: HHEM-2.1-open scorer

**Status.** HHEM passed the model-card compatibility check on a Colab Tesla T4. The scorer is pinned and code-reviewed, and the export and import round trip is covered by synthetic tests.
- **Dev scoring is done** (2026-11-26). After the 37 dev pairs received agreed human labels, HHEM scored them on a Colab T4 alongside the DeBERTa and Qwen3-8B judge baselines: **[m7-baseline-run.md](m7-baseline-run.md)** (aggregates only).
- **Out of scope:** no fine-tuning; no threshold was chosen.

## Revision 2 (PR #14 review)

1. **The runtime is checked before any download.** The order is now:
   - the inputs are verified;
   - the runtime must be **Google Colab, with torch, CUDA and a Tesla T4** (the measured target), otherwise the scorer exits with code 2 **without calling `fetch`**;
   - the pinned files are downloaded and verified;
   - the files are staged into load directories.

   `--allow-cpu` is removed. Other GPUs (A100, L4) are refused, so latency is always measured on the target.
2. **Only hash-verified files are loaded.**
   - **Clean staging:** after download, only the pinned files are copied into fresh, empty load directories (`<cache>/load/hhem`, `<cache>/load/flan-t5-base`, recreated each run) and re-verified there.
   - **Exact contents:** the backend refuses any load directory whose contents are not exactly the pinned files.
   - **Offline:** loading stays offline (`HF_HUB_OFFLINE=1`, `local_files_only=True`).
   - **Private module cache:** `trust_remote_code` modules are imported from a private, empty `HF_MODULES_CACHE`, so only the verified code can be imported.
   - **Why it matters:** with the pinned transformers 4.56.2 tokenizer, an extra `added_tokens.json` beside the flan-t5-base files changes the token ids (28 → 30 tokens on the HHEM prompt). A real-tokenizer test shows that staging removes this effect.

## Colab T4 compatibility smoke test

**Passed.** On November 26, 2026, Colab ran commit `4f06c2597628d376ae242bb3659aaabd63c25193`. All seven public examples matched their published scores within 0.005 and yielded the same decisions on the warm-up and all five timed passes. The [original JSON report](../evidence/m7/hhem-smoke-report.json) records every score and latency.

| Measurement | Result |
| --- | --- |
| Runtime | Tesla T4, NVIDIA driver `580.82.07`, CUDA `13.0`, torch `2.14.1+cu130` |
| Libraries | Python `3.13.16`, transformers `4.56.2`, tokenizers `0.22.2`, safetensors `0.8.0` |
| Maximum absolute score deviation | `3.58 × 10⁻⁷` |
| Model load time | 19.98 s |
| Timed latency across 35 predictions | 21.44 ms median, 32.21 ms p95, 37.28 ms max |

* **Notebook:** `ml/hhem-baseline/hhem_smoke_t4.ipynb`. Open it in Colab, choose *Runtime → Change runtime type → T4 GPU*, then *Run all*. To reproduce this result, set `REF` to the full commit above. The smoke cell preserves the exit code and checks that the report matches the current attempt and commit. If the smoke cell fails but produces a verified report, run the last two cells to inspect and download it.
* **Script:** `ml/hhem-baseline/smoke_model_card.py`, which uses only the merged scorer (`hhem_score.py`):
  1. requires Colab + torch + CUDA + a Tesla T4 (exit 2 otherwise, before any download);
  2. requires `transformers==4.56.2`, `tokenizers==0.22.2` and `safetensors==0.8.0` (exit 2 otherwise);
  3. downloads, verifies and stages the pinned files, then loads HHEM offline through the reviewed code;
  4. scores the **7 public examples** from the model card at `de9a5e6b` (README SHA-256 `85f87c95…`). Each example must match the published `consistent` score within 0.005 and yield the same > 0.5 decision during warm-up and on all five timed passes;
  5. records the GPU, NVIDIA driver, torch, CUDA, library versions, code commit, model load time, and each example's score and latency on every pass.

  The script exits with 0 for a pass, 1 for a mismatch, 2 for an unsupported runtime or version, or 3 for a download, staging, load or scoring error. An execution error report includes the stage, exception type and reason.
* **No M7 data:** only the public examples are used. Nothing is fine-tuned and no threshold is chosen.
* **Local regressions** use synthetic stand-ins. They cover score drift on a timed pass after a passing warm-up, replacement of an earlier success report after a load error, failed notebook checkout or install, and missing or current failure reports. From `backend`, `pytest -q tests/unit/test_m7_evaluation.py -k 'smoke or notebook'` passed all 14 selected tests. The full backend unit suite passed 614 tests.
* **Local run of the script:** status `unsupported` ("not a Google Colab runtime"), exit 2, nothing downloaded. This is the expected result off Colab.

## What it does

`ml/hhem-baseline/hhem_score.py`:

1. **Reads** the JSONL from `python -m app.evaluation checker-baselines --export-inputs FILE`. It verifies every row's `input_sha256` (SHA-256 of id, claim and premise) and the header's `inputs_sha256`, using the same formulas as the runner (a parity test enforces this). Edited or stale inputs are refused **before any download or scoring**.
2. **Fetches and verifies** the pinned files (below). It loads HHEM's custom code only if its bytes equal the reviewed code, and refuses any other `.py` file in the model directory.
3. **Scores** each pair once as `(premise, claim)` with the model's own `predict()`, timing each call with CUDA synchronisation.
4. **Writes** a predictions file that the runner accepts:
   - **Header:** `model`, `revision`, `inference: hhem-2.1-open`, the export's `inputs_sha256` (copied unchanged), the input file's SHA-256, and the operating point.
   - **`runtime` in the header:** Python, platform, torch, transformers, CUDA, device name, the scorer's own SHA-256, the reviewed code and weights hashes, and the foundation revision.
   - **Rows:** one per pair, with its `input_sha256` (copied unchanged), `p_supported`, `predicted` and `latency_ms`.

**Decision rule:** the model's own two-label output. `supported` iff P(consistent) > 0.5, which is the argmax, with an exact tie going to `hallucinated`. It is recorded as the model's convention; **no threshold is selected**.

## Pins

| Artifact | Revision | SHA-256 |
| --- | --- | --- |
| `vectara/hallucination_evaluation_model` | `de9a5e6ba9ba49531a022dba17e35ffeb2a6fad8` | |
| `config.json` | | `773139fe…cc870` |
| `configuration_hhem_v2.py` (reviewed) | | `ec57fe34…cacc7` |
| `modeling_hhem_v2.py` (reviewed) | | `f1fcfd46…a262` |
| `model.safetensors` (438.5 MB) | | `634de18a…1e72` (Hub LFS hash) |
| `google/flan-t5-base` (foundation: config and tokenizer only) | `7bcac572ce56db69c1ea7c8af255c5d7c9672fc2` | `config.json` `7c1853db…`, `spiece.model` `d60acb12…` (matches the Hub LFS hash), `tokenizer.json` `fe2ebbbb…`, `tokenizer_config.json` `4c551244…`, `special_tokens_map.json` `5c87151e…` |

The full hashes are in `hhem_score.py`. The Colab environment is `ml/hhem-baseline/requirements.txt`: `transformers==4.56.2` (as elsewhere in the repo) plus the Colab torch build.

## Review of the custom code (at `de9a5e6b`)

The model loads with `trust_remote_code`, so the two Python files run inside our process. I read both files in full.

* **`configuration_hhem_v2.py`** (760 bytes) defines `HHEMv2Config` with class attributes:
  - `foundation = "google/flan-t5-base"`;
  - the prompt `"<pad> Determine if the hypothesis is true given the premise?\n\nPremise: {text1}\n\nHypothesis: {text2}"`.

  The constructor is misspelled `__init___`, so the class attributes are always used (the file's own FIXME notes this). This is harmless, and our foundation redirect relies on it.
* **`modeling_hhem_v2.py`** (2,767 bytes) defines `HHEMv2ForSequenceClassification`:
  - it wraps `T5ForTokenClassification(AutoConfig.from_pretrained(config.foundation))` and loads `AutoTokenizer.from_pretrained(config.foundation)`;
  - `predict()` formats the prompt (premise = text1, hypothesis = text2), tokenizes with padding and **no truncation**, and returns softmax P(label 1 = "consistent") of the first token;
  - `forward()` runs under `no_grad`. `populate()` is a publishing helper we never call.
* **Safety:**
  - no `exec` or `eval`, subprocess, OS, file or network calls of its own; it imports only `torch` and `transformers`;
  - the commented-out code (PEFT/LoRA) is inactive.
* **Finding: an unpinned dependency.** The reviewed code fetches `google/flan-t5-base` config and tokenizer **without a revision** every time the model is built. The scorer therefore:
  - downloads them at a pinned revision and verifies them;
  - points `foundation` at that local copy;
  - loads with `HF_HUB_OFFLINE=1` and `local_files_only=True`, so nothing unpinned is fetched.
* **Finding: version provenance.** `config.json` says it was saved with transformers 4.39.3, while the code has a `post_init()` added "for transformers>=5". The Colab T4 smoke test confirmed loading under 4.56.2 and reproduced the model card's example scores.
* **No truncation:** HHEM-2.1-open has no 512-token limit (per the model card), so long premises are scored in full.

## Tests (synthetic only; `backend/tests/unit/test_m7_evaluation.py`)

* **Digest parity:** the scorer's `input_digest` and `inputs_digest` equal the runner's (Unicode included).
* **Round trip:**
  - test-only agreeing labels → `export_inputs` → `read_inputs` → `score_inputs` with a synthetic backend → runner `PredictionsScorer` → `run()` gives `status: scored` (37 pairs);
  - the header and every row keep the export's hashes exactly;
  - the model, revision, reviewed code hashes and foundation revision are recorded.
* **Synthetic files are not results:** the runner refuses `inference: synthetic` unless a test passes `allow_synthetic=True`. The CLI never does.
* **Stale inputs:** the scorer refuses an edited claim or premise, a wrong header digest, a dropped or duplicated row, and a non-export file. With stale inputs, `main()` downloads nothing and writes nothing.
* **Stale predictions:** predictions made before a premise changed under the same id are rejected by the runner.
* **Code gate:**
  - placeholder bytes under the reviewed names are refused, and the backend refuses **before importing torch or transformers**;
  - extra `.py` files are refused;
  - a weights hash mismatch is refused.
* **Decision rule:** 0.5 → unsupported, just above 0.5 → supported; scores outside [0, 1] fail.
* **Runtime gate (revision 2):** this test machine (not Colab, no torch) exits with code 2 without calling `fetch`. So does each simulated unsupported runtime: not Colab, no torch, no CUDA, A100, L4. A supported T4 runtime is checked after the inputs and before `fetch`. `--allow-cpu` is rejected.
* **Clean staging (revision 2):**
  - only pinned files are staged, and a stale extra file in an old load directory is removed;
  - a tampered source file fails staging;
  - `main()` hands the backend only the staged directories, not the download directories that hold an extra `added_tokens.json` and `extra.py`;
  - the backend refuses a load directory with unexpected files, before importing torch.
* **Real tokenizer (CI job `checker-tokenizer`, `ml/hhem-baseline/tests/test_tokenizer_isolation.py`):** this uses the pinned flan-t5-base files and transformers 4.56.2. An `added_tokens.json` loaded directly changes the ids; after staging the ids equal the clean baseline.

## Reproduction

Locally:

```bash
cd backend
.venv/bin/pytest -q tests/unit/test_m7_evaluation.py -k "hhem or stale or unreviewed or extra_python or pinned_file"
```

Once human labels exist:

1. **Laptop: export the inputs.**
   ```bash
   cd backend
   .venv/bin/python -m app.evaluation checker-baselines --export-inputs ../var/m7-checker-inputs.jsonl
   ```
2. **Colab (GPU runtime):** copy `ml/hhem-baseline/` and the inputs file, then:
   ```bash
   pip install -r requirements.txt
   python hhem_score.py --inputs m7-checker-inputs.jsonl --out hhem-predictions.jsonl --cache-dir /content/hhem-pinned
   ```
3. **Laptop: score.**
   ```bash
   cd backend
   .venv/bin/python -m app.evaluation checker-baselines --scorer predictions --predictions ../var/hhem-predictions.jsonl --out ../var/m7-baseline-hhem.json
   ```

## Gaps

1. **Human labels.** Dev scoring can proceed once the 37 pairs are independently reviewed and disagreements are resolved.
2. **Dev latency.** The scorer records CUDA-synchronised inference time per pair. The smoke results above cover the model-card examples. Dev timings will come from the labelled-set run.
3. **Out of scope.** Fine-tuning and threshold selection.
