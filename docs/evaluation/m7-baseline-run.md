# M7 checker baselines: first real comparison on the 37 human-agreed dev pairs

Status (2026-11-26): **dataset verified; all three model runs PENDING.** They need the owner's Colab T4 sessions (blockers below). No result is reported until the raw reports come back. The synthetic self-test is not a result and is not in the table.

## 1. Dataset (verified on the laptop)

| Item | Value |
| --- | --- |
| Labels | Local commit `28d727b` (branch `feature/m7-dev-human-labels`, **not pushed**): `gold-ann-a.csv`, `gold-ann-b.csv` |
| Human review | 2 annotators, 37/37 double-labelled, **37 agreed**, 0 adjudicated, Cohen's kappa **1.00** |
| Gold classes | 18 supported, **19 unsupported** |
| Dataset revision (gold pair file) | `cc05512a3fc4ed6a50823846f7f058bd5386b52875198706bc7345e979db32c6` |
| Active manifest | `m7-split-v3` (`be2205db…`); every pair's evidence is checked against the frozen dev selection |
| Exported inputs | 37 pairs, `inputs_sha256` **`7ad97fc97b42ecc213315d452964dcf2bff00d12a5c46cfa3a22b8adcbf48f9b`** |
| Export file SHA-256 | `97bd0387058b0d2aab006bee84b11a43fe4d38e20e85c5a32f2f9b205c59c1ec` (a clean `main` checkout plus the two label files reproduces it byte for byte) |

## 2. Runs and blockers

| Baseline | Where it must run | Status | Exact blocker |
| --- | --- | --- | --- |
| Zero-shot DeBERTa (`MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2`, threshold 0.5) | Project gateway in a Colab T4 runtime, runner over loopback | **PENDING** | No gateway reachable from this laptop. `.env` has no `LLM_BASE_URL` or `MODEL_GATEWAY_TOKEN`. The gateway exists only in an owner-started Colab GPU session, and a laptop→Colab tunnel has an open Colab usage gate (`ml/model-gateway/README.md`). |
| Qwen3-8B judge (`Qwen/Qwen3-8B-AWQ@4da05a8e`, prompt `m7-judge-1`) | Same | **PENDING** | Same |
| HHEM-2.1-open (`de9a5e6b`, argmax) | Colab T4 (merged scorer) | **PENDING** | No Colab T4 available to the agent |

### Owner steps

**A. Gateway baselines.** `ml/colab-m7/m7_baselines_gateway_t4.ipynb`, about 25–35 minutes on a T4:
- It installs the hash-locked backend and the pinned vLLM environment, and launches the gateway on loopback.
- It takes the two imported label files as an upload into `/content/m7-labels`. They are passed with `--label-dir` and never written into the repository. Never upload the reviewers' filled sheets or the coordinator keys.
- It re-checks `inputs_sha256` and the export SHA-256 above, runs `checker-baselines --scorer deberta` and `--scorer qwen-judge`, and asserts the dataset revision.
- **Return** `m7-baselines-gateway-<timestamp>.zip`. It holds the two reports, the gateway `/health`, the export and the versions; it has no CSVs and no token.

**B. HHEM.** `ml/hhem-baseline/hhem_score_t4.ipynb`, about 5 minutes on a **fresh** T4 runtime:
- Upload `var/m7-baselines/m7-checker-inputs.jsonl`; the notebook verifies its SHA-256 and `inputs_sha256`.
- It runs the merged `hhem_score.py`.
- **Return** `hhem-predictions.jsonl`.

**C. Laptop import.** Put the returned files in `var/m7-baselines/`, then:

```bash
git switch feature/m7-dev-human-labels   # the label files are only on this local branch
cd backend
unzip -o ../var/m7-baselines/m7-baselines-gateway-*.zip -d ../var/m7-baselines/gateway
.venv/bin/python -m app.evaluation checker-baselines --scorer predictions \
  --predictions ../var/m7-baselines/hhem-predictions.jsonl --out ../var/m7-baselines/hhem.json
```

The import rejects HHEM predictions unless the header `inputs_sha256` and every row's `input_sha256` match these 37 inputs.

## 3. Comparison (to be filled from the raw reports)

| Baseline | Unsupported P | Unsupported R | Unsupported F1 | False-support rate (95% CI) | ECE | Brier | p50 latency | p95 latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Zero-shot DeBERTa | pending | pending | pending | pending | pending | pending | pending | pending |
| Qwen3-8B judge | pending | pending | pending | pending | n/a (hard labels) | pending | pending | pending |
| HHEM-2.1-open | pending | pending | pending | pending | pending | pending | pending | pending |

**What each latency includes**:
* **DeBERTa:** wall time around one `GatewayModelClient.check()` call from the runner, through loopback HTTP to the gateway: JSON, auth, tokenization, fp16 forward pass on the T4 and response. vLLM shares the GPU.
* **Qwen judge:** wall time around one streamed chat call (loopback): prompt processing and up to 8 output tokens under vLLM 0.11.0, AWQ, 0.75 GPU memory.
* **HHEM:** wall time around one `predict()` call per pair, CUDA-synchronised, in-process (no HTTP), on a dedicated T4 runtime. It excludes model load and download.

The three measurements are therefore not identical in scope. DeBERTa and Qwen include gateway HTTP; HHEM does not.

## 4. What 37 pairs can establish

19 unsupported claims bound the headline metric. Wilson 95% intervals for the false-support rate:

| False passes | Rate | 95% CI |
| --- | --- | --- |
| 0/19 | 0.000 | [0.000, 0.168] |
| 1/19 | 0.053 | [0.009, 0.246] |
| 2/19 | 0.105 | [0.029, 0.314] |
| 3/19 | 0.158 | [0.055, 0.376] |
| 5/19 | 0.263 | [0.118, 0.488] |

* Even a perfect score cannot show a false-support rate below about 17% with 95% confidence. Two baselines would need to differ by roughly 5 or more false passes before their intervals separate.
* The pairs are **template perturbations** from six dev families: number, unit, entity, scope and polarity changes, plus originals. Results describe these change types, not real answer sentences. Per-change-type accuracy rests on 1–9 pairs per type.
* Inter-annotator agreement was perfect. That suggests the labels are unambiguous, and also that the pairs are easy for humans, so they may under-represent hard cases.
* These are **dev** results, and dev data must not be used to choose among models and then report the winner as final. The release claim needs the blind release set (coordinator procedure in `m7-v3-corpus.md`).

## 5. Error patterns

Pending, to be written from the per-pair predictions in the raw reports: false passes by change type, false alarms on supported originals, judge parse failures, and calibration bins.

## 6. Privacy

The reviewer sheets and coordinator keys stay in `var/m7-labels` (git-ignored) and are never uploaded. The imported label files exist only in local commit `28d727b` and in the owner's private Colab runtime. Pushing that commit or per-pair reports to the public repository needs the owner's decision.
