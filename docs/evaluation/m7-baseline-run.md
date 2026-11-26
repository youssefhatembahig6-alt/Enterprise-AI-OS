# M7 checker baselines: first real comparison on the 37 human-agreed dev pairs

Status (2026-11-26): **complete.** All three specified baselines scored the same 37 human-agreed dev pairs on Colab Tesla T4s. The raw reports were verified independently: checksums, served revisions, input digests and metrics recomputed from the per-pair predictions. This document reports **aggregates only**. The per-pair reports, the label files and labels commit, the reviewer worksheets and the coordinator keys stay private and are not in this repository.

No fine-tuning, no threshold selection and no blind release evaluation were done. The synthetic self-test is not a result and is not in the table.

## 1. Dataset

| Item | Value |
| --- | --- |
| Human review | 2 annotators, 37/37 double-labelled, **37 agreed**, 0 adjudicated, Cohen's kappa **1.00** |
| Gold classes | 18 supported, **19 unsupported** |
| Dataset revision (gold pair file) | `cc05512a3fc4ed6a50823846f7f058bd5386b52875198706bc7345e979db32c6` |
| Active manifest | `m7-split-v3` (`be2205db…`); every pair's evidence lies in the frozen dev selection |
| Scored inputs | 37 pairs, `inputs_sha256` **`7ad97fc97b42ecc213315d452964dcf2bff00d12a5c46cfa3a22b8adcbf48f9b`**; export file SHA-256 `97bd0387058b0d2aab006bee84b11a43fe4d38e20e85c5a32f2f9b205c59c1ec` |
| Labels | Kept private: a local commit plus the owner's Colab runtime, passed with `--label-dir` |

## 2. What ran, and how it was verified

| Baseline | Model and revision (verified) | Decision rule (existing) | Where |
| --- | --- | --- | --- |
| Zero-shot DeBERTa | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2b59cabb106aca4c287eed12e357e90eb` | supported iff P(entailment) ≥ 0.5 (configured `CHECKER_ENTAILMENT_THRESHOLD`, uncalibrated, not selected) | Project gateway, Colab T4, runner over loopback |
| Qwen3-8B judge | `Qwen/Qwen3-8B-AWQ@4da05a8edb55c6046cce958586c33b61da07bb79` | one-word answer, prompt `m7-judge-1` (SHA-256 `b7578d7f…`), temperature 0, thinking off; unparseable = unsupported | Same gateway |
| HHEM-2.1-open | `vectara/hallucination_evaluation_model@de9a5e6ba9ba49531a022dba17e35ffeb2a6fad8`, foundation `google/flan-t5-base@7bcac572` | supported iff P(consistent) > 0.5 (the model's argmax) | Merged scorer, separate Colab T4 runtime |

**Verification (done on the laptop):**
* **Gateway `/health`:** chat `Qwen/Qwen3-8B-AWQ@4da05a8e…` and checker `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2…`, with vLLM 0.11.0, AWQ, fp16, `max_model_len` 4096 and GPU memory utilization 0.75. The runner also verified the served identity before sending any pair. Both reports record `inference: gateway`.
* **Runtimes:**
  - Gateway runtime: Tesla T4 (driver 580.82.07, 15,360 MiB), vLLM env (vllm 0.11.0, transformers 4.56.2, torch 2.8.0+cu128), backend Python 3.13.16, application commit `c88d5dd151be396460bf1cea134b3d00c4e7ccee`.
  - HHEM runtime: Tesla T4, torch 2.14.1+cu130 (CUDA 13.0), transformers 4.56.2, Python 3.13.16, scorer SHA-256 `9c2c4877…`.
* **Inputs:** the gateway bundle's export is byte-identical to the laptop export. The HHEM predictions carry the same `inputs_sha256`, and the runner's import verified it plus all 37 per-pair `input_sha256` values. An independent re-import reproduced the saved HHEM metrics exactly.
* **Labels:** all three reports have the dataset revision and label-file hashes of the local labels, and cover exactly the 37 pairs.
* **Metrics:** every metric was recomputed from the per-pair predictions with `checker_metrics.summarize`, and all are identical to the reports.

Private artifact SHA-256s (files not published): gateway bundle `59cd43c5…`, `deberta.json` `99c9d7ce…`, `qwen-judge.json` `501005c0…`, `gateway-health.json` `d89fe122…`, `versions.json` `e87340f0…`, `hhem-predictions.jsonl` `9e76e60c…`, `hhem.json` (runner import) `fbff21ac…`.

## 3. Comparison (37 dev pairs; "unsupported" is the positive class)

| Baseline | TP / FP / FN / TN | Unsupported precision | Unsupported recall | Unsupported F1 | False-support rate (Wilson 95% CI) | ECE | Brier | Median latency | p95 latency |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Zero-shot DeBERTa | 18 / 3 / 1 / 15 | 85.7% | 94.7% | **90.0%** | **1/19 = 5.3%** [0.9%, 24.6%] | 0.096 | 0.088 | 30.5 ms | 396.8 ms |
| Qwen3-8B judge | 19 / 1 / 0 / 17 | 95.0% | 100.0% | **97.4%** | **0/19 = 0.0%** [0.0%, 16.8%] | 0.027* | 0.027* | 806.5 ms | 25,791.9 ms |
| HHEM-2.1-open | 18 / 2 / 1 / 16 | 90.0% | 94.7% | **92.3%** | **1/19 = 5.3%** [0.9%, 24.6%] | 0.118 | 0.070 | 14.7 ms | 23.4 ms |

\* The judge outputs hard labels (P(supported) is 1 or 0), so its calibration numbers only restate its accuracy. They are not probability calibration.

The judge returned 0 unparseable answers. F1 = 2TP / (2TP + FP + FN). Latency is nearest-rank over the 37 calls. The maximum calibration error is kept in the private reports only: with 37 pairs a calibration bin can hold a single pair, so that figure can equal one pair's exact score.

### What each latency includes
* **DeBERTa:** wall time around one `GatewayModelClient.check()` call from the runner, through **loopback HTTP** to the gateway: JSON, auth, tokenization and the fp16 forward pass on the T4, with vLLM resident on the same GPU.
* **Qwen judge:** wall time around one **streamed chat call** through loopback: prompt processing and up to 8 output tokens under vLLM (AWQ, 0.75 GPU memory).
* **HHEM:** wall time around one in-process `predict()` call, **CUDA-synchronised, no HTTP**, on a dedicated T4 runtime. It excludes download and model load.
* **How p95 arises in this run (observations).** Latency is nearest-rank over 37 calls, so p95 is the 36th-smallest value. The runner had **no warm-up pass**.
  - **DeBERTa:** the first two calls were the slowest (739.8 ms and 396.8 ms). The second call determines the p95 (396.8 ms).
  - **Qwen judge:** the first two calls were the slowest (33.6 s and 25.8 s). The second call determines the p95 (25,791.9 ms). One later call also exceeded 1 s.
  - **HHEM:** the first call was the maximum (422.0 ms). The p95 (23.4 ms) is determined by call 24.
  - These early spikes are observations; this run did not isolate their cause.

  The three measurement scopes differ (loopback HTTP vs in-process), so compare latency within a scope, not across scopes.

## 4. Error patterns (aggregate)

| Baseline | False passes (unsupported judged supported) | False alarms (supported judged unsupported) |
| --- | --- | --- |
| Zero-shot DeBERTa | 1 of 19 | 3 of 18 |
| Qwen3-8B judge | 0 of 19 | 1 of 18 |
| HHEM-2.1-open | 1 of 19 | 2 of 18 |

* **False passes:** the two observed false passes (one each for DeBERTa and HHEM) were both value substitutions (unit or entity edits). All ten number, polarity and scope edits were caught by all three models.
* **False alarms:** all of them were on supported originals, and some were shared across models. False alarms outnumber false passes for every model.
* **Thresholds:** this run used each baseline's existing decision rule only. It did not test alternative thresholds, and threshold selection is a separate, dev-only step.

## 5. What 37 pairs can establish

* **The observed Wilson 95% intervals for the false-support rate overlap.** DeBERTa and HHEM are at 1/19 [0.9%, 24.6%]; the judge is at 0/19 [0.0%, 16.8%]. Even 0/19 false passes still has an upper bound of **16.8%**.
* **These are per-model descriptions of a small sample, not a paired significance test.** No ranking of the three baselines is established.
* **The pairs are template edits** from six dev document families. Some change types have only one or a few pairs, and errors can cluster within a family. Results describe these change types, not live answer sentences.
* **Agreement was perfect** (kappa 1.00). The labels are unambiguous, but the pairs may be easy and under-represent hard cases.
* **These are dev results.** They must not be used to pick a model and then report it as final. The release claim needs the blind release set (coordinator procedure in `m7-v3-corpus.md`).
* **What the sample does show:** all three pinned baselines run end to end in the approved Colab setup with verified identities and digests, and they catch most single-fact edits. The observed failure types (value substitutions passed, supported originals rejected) are candidates to examine in a larger human-labelled dev set, which is a separate assignment.

## 6. Reproduction

1. **Gateway baselines:** run `ml/colab-m7/m7_baselines_gateway_t4.ipynb` (notebook revision `58ed84e`) on a Colab **T4** with `REF = "c88d5dd151be396460bf1cea134b3d00c4e7ccee"`.
   - Upload the two imported label files at cell 3; they go to `/content/m7-labels` and are passed with `--label-dir`. Never upload the reviewer worksheets or coordinator keys.
   - The notebook asserts 37 pairs, `inputs_sha256`, the export SHA-256 and the dataset revision.
   - It returns `m7-baselines-gateway-<timestamp>.zip` (two reports, gateway health, export, versions).
2. **HHEM:** run `ml/hhem-baseline/hhem_score_t4.ipynb` on a **fresh** T4 runtime with the same `REF`. Upload the laptop export `m7-checker-inputs.jsonl`, which is verified by SHA-256. It returns `hhem-predictions.jsonl`.
3. **Laptop import** (with the labels checkout):

   ```bash
   cd backend
   .venv/bin/python -m app.evaluation checker-baselines --export-inputs ../var/m7-baselines/m7-checker-inputs.jsonl   # inputs_sha256 7ad97fc9…
   unzip -o ../var/m7-baselines/m7-baselines-gateway-*.zip -d ../var/m7-baselines/gateway
   .venv/bin/python -m app.evaluation checker-baselines --scorer predictions \
     --predictions ../var/m7-baselines/hhem-predictions.jsonl --out ../var/m7-baselines/hhem.json
   ```

   The import rejects predictions unless `inputs_sha256` and all 37 per-pair digests match.
4. **Recompute the table:** load each report's `predictions` and call `app.evaluation.checker_metrics.summarize` with the local gold labels. The result is identical to each report's `metrics`.

All model inference ran only in the project's Colab setup: the gateway over loopback (no tunnel), and HHEM through the pinned, staged and offline scorer.
