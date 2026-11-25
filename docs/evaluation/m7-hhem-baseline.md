# M7 baseline: HHEM-2.1-open scorer

Status: **scorer built, pinned and code-reviewed; round trip tested with synthetic data.**
- **No real HHEM run yet.** Real scoring is blocked: no dev pair has human labels, so `--export-inputs` has nothing to export. HHEM also runs only on the Colab GPU (project rule: no models on laptops).
- **Out of scope:** no fine-tuning; no threshold was chosen.

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
* **Finding: version provenance.** `config.json` says it was saved with transformers 4.39.3, while the code has a `post_init()` added "for transformers>=5". Loading under 4.56.2 has **not been verified yet**. The first Colab run must confirm it, and should reproduce the model card's example scores as a smoke test.
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

1. **Human labels:** real scoring is blocked until the 37 dev pairs are labelled.
2. **First real Colab run:** confirm loading under transformers 4.56.2 and reproduce the model card's example scores. Then record the runtime the header captures (GPU, CUDA, torch) and latency per claim.
3. **Latency** is measured around `predict()` per pair, CUDA-synchronised. Model load time is excluded and not yet reported.
4. **Out of scope by instruction:** fine-tuning and threshold selection.
