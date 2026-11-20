# M1 GPU feasibility benchmark

[`m1_gpu_feasibility.ipynb`](m1_gpu_feasibility.ipynb) checks whether Qwen3-8B-AWQ on vLLM, BGE-M3 and the DeBERTa-v3 checker can run together on one Colab T4. It times the ten-question M1 workload. This covers the specification's open questions in section 13 (startup at 4,096 tokens, co-residency, and seconds per answer) and the GPU report in the roadmap's M1 section.

Open in Colab: <https://colab.research.google.com/github/youssefhatembahig6-alt/Enterprise-AI-OS/blob/feature/m1-gpu-feasibility/ml/gpu-feasibility/m1_gpu_feasibility.ipynb>

After this branch is merged, replace `feature/m1-gpu-feasibility` with `main` in that link.

## Run it

1. Open the link and sign in to Google. Choose **File → Save a copy in Drive** so your edits and outputs belong to you.
2. Choose **Runtime → Change runtime type → T4 GPU → Save**.
3. Choose **Runtime → Run all**. If no GPU is attached, cell 1 stops before anything is installed or downloaded.
4. Approve the Google Drive prompt from cell 3. Results are then mirrored to `MyDrive/eaios-m1-gpu-feasibility/<run id>/` after every stage. This copy survives a disconnected session. If you decline, the run continues and the results stay in the runtime.
5. Keep the tab open and active. The run takes about 60 to 90 minutes: roughly 5 minutes to install, 5 to download 9 GB, then two configurations of about 25 minutes each. Free Colab can disconnect idle tabs.
6. Cell 10 downloads `eaios-m1-gpu-<run id>.zip`. You can run cell 10 at any time, including after a stage fails. Return the zip, or the Drive folder, for review.

If the session disconnects, take the partial `results.json` from Drive. A new run starts a fresh run ID.

## What it measures

| Cell | Stage | Recorded |
| --- | --- | --- |
| 1 | GPU and memory detection | GPU name, compute capability, driver and CUDA version, GPU, host and disk memory, and warnings |
| 4 | Install | vLLM 0.11.0 in its own uv virtual environment, isolated from Colab's torch, with package versions and a full freeze |
| 5 | Download | Repository, requested revision, resolved commit SHA, files, bytes, seconds and license for each model |
| 8 | Each `gpu_memory_utilization` candidate (0.85 from section 6, then 0.75 from section 13) | See below |

For each candidate, the notebook:

* starts vLLM and records the seconds until it is ready, the free memory beforehand, and lines from the server log covering the KV cache, maximum concurrency, attention backend and any OOM
* loads BGE-M3 (dense and sparse heads, fp16) and the checker into the notebook kernel next to the running server
* runs a warm-up that is left out of the statistics
* makes two passes over the ten questions, each through the full chain:
  * database questions: router, then SQL, then report, then checks
  * document questions: router, then query embedding, then grounded answer, then checks
  * per step: time to first token, total seconds, token counts, decode tokens per second, and checker latency for each claim and each answer sentence
* runs four chat requests at once while an embedding and checker loop runs, to put all three models under load together
* cancels a streaming request partway through, then confirms that `vllm:num_requests_running` returns to 0 and that the next request still succeeds
* restarts the server on the first candidate. It first tries with the auxiliary models still loaded. If that fails, it unloads them, restarts and reloads them, and records which recovery was needed.
* polls NVML every 100 ms throughout and records peak GPU use, minimum headroom, the kernel's torch allocator peaks and peak host RAM

`summary.md` reports a pass or fail line and latency tables (median, p90 and max over the warm pass) for each candidate. It also names a suggested candidate: the configuration that passed every check with the most headroom. Choosing the configuration remains a project decision.

## Limits of this evidence

* **Checker:** `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` is a public zero-shot NLI checkpoint, used here as a development baseline behind the checker interface. It is not the fine-tuned checker from M7. Its agreement with the expected labels is a smoke count, not an accuracy result. The support threshold of 0.5 is uncalibrated.
* **Workload:** the specification asks for timings on "10 NileTech questions" but does not list them. `m1-gpu-workload-v1` is a proposed frozen set built from the M1 sales fixtures and the NileTech 0.1.6 policy and contract documents. Its SHA-256 is recorded with each run. It is not the evaluation set.
* **Fixed evidence:** generation uses fixed evidence, namely the frozen Q1 2026 figures and gold passages, so timings stay comparable. The generated SQL is recorded but not executed. Retrieval runs only as a smoke ranking over ten passages.
* **Network:** vLLM listens on `127.0.0.1` with a random per-run API key that is never saved. No tunnel is opened, so the run stays within interactive notebook use. Whether a live tunnelled gateway is acceptable under Colab's terms is a separate gate (see the roadmap).
* **Model revisions:** each model resolves to a commit SHA at run time. To repeat an earlier run exactly, set the recorded SHAs in cell 2.

## If a stage fails

The failure is recorded in `results.json` (under `failures` and in that stage's `error` and `traceback`) and in the vLLM log in the zip, and the run carries on where it can. Return the zip either way, because a failure is itself a result. To retry with changed settings, edit cell 2 and run **Runtime → Run all** again. Settings you can change there:

* `vllm.enforce_eager: True`, if CUDA-graph capture fails or runs out of memory
* `vllm.attention_backend: "TRITON_ATTN"` or `"FLEX_ATTENTION"`, if the default attention backend is rejected on the T4
* `vllm_version`, to compare another pinned release

## Editing

The notebook is generated. Edit `build_notebook.py`, then run `python ml/gpu-feasibility/build_notebook.py`.
