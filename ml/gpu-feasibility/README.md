# M1 GPU feasibility benchmark

[`m1_gpu_feasibility.ipynb`](m1_gpu_feasibility.ipynb) runs on a free Colab T4 in one of two modes, chosen in cell 2. Each run records **one** mode. The summary, the zip name and the Drive folder are all labelled with that mode, so small and target results are always reported separately.

| Mode | Models (only these are downloaded) | What runs | Time |
| --- | --- | --- | --- |
| **`small`** (default) | `Qwen/Qwen3-1.7B` (vLLM, fp16), `BAAI/bge-m3`, `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` | One NileTech document question (Q05, annual leave in Egypt): BGE-M3 query embedding → answer generation → DeBERTa checking | About 15–25 min, about 6 GB downloaded |
| `target` | `Qwen/Qwen3-8B-AWQ` (vLLM, AWQ), `BAAI/bge-m3`, the same DeBERTa checkpoint | The ten timing questions ×2, concurrent load, cancellation and restart, at `gpu_memory_utilization` 0.85 and 0.75 | About 70–100 min, about 9 GB downloaded |

Small mode is a smoke run of the full path on the same GPU. It is **not** a measurement of the target model. It makes no configuration recommendation and must not be compared with target numbers. Target mode answers section 13's open questions: whether Qwen3-8B-AWQ starts at 4,096 tokens, whether the three models fit together, and how many seconds an answer takes.

Open in Colab: <https://colab.research.google.com/github/youssefhatembahig6-alt/Enterprise-AI-OS/blob/feature/m1-gpu-feasibility/ml/gpu-feasibility/m1_gpu_feasibility.ipynb>

After this branch is merged, change `feature/m1-gpu-feasibility` to `main` in that link.

## Retry after the Transformers startup failure

The first free-Colab run failed at vLLM startup with `AttributeError: Qwen2Tokenizer has no attribute all_special_tokens_extended`. The cause: vLLM 0.11.0 declares `transformers>=4.55.2` with no upper bound, so the install resolved Transformers 5.23.0, which removed that attribute.

The fix has three parts:

* **Pins.** [`vllm-env-pins.txt`](vllm-env-pins.txt) now pins `vllm==0.11.0` and `transformers==4.56.2`.
* **Repair on rerun.** Cell 4 reuses an existing `/content/vllm-env` only if every pin matches exactly. Otherwise it repairs the environment in place, or rebuilds it if the repair fails.
* **Preflight.** A new stage, `vllm_tokenizer_preflight`, at the end of cell 5 initialises the chat model's tokenizer through vLLM's own `get_tokenizer()` before any server starts.

You don't need to delete anything. The same runtime, or a fresh one, both work.

## Run the small mode (default)

1. Open the link, sign in to Google, and choose **File → Save a copy in Drive**.
2. Choose **Runtime → Change runtime type → T4 GPU → Save**.
3. Leave cell 2 at `MODE = "small"`.
4. Choose **Runtime → Run all**. Cell 1 shows the GPU and memory, and stops before installing or downloading anything if no GPU is attached.
5. Approve the Google Drive prompt from cell 3. Results are then copied to `MyDrive/eaios-m1-gpu-feasibility/small/<run id>/` after every stage. If you decline, the run continues and the results stay in the runtime.
6. Keep the tab open until cell 10 finishes. Cell 10 downloads `eaios-m1-gpu-small-<run id>.zip`, which contains `results.json`, `summary.md`, `workload.json`, the vLLM log, the vLLM venv freeze and the install log. Cell 10 is safe to run at any time, including after a failure.

## Run the target mode (explicit)

Follow the same steps, but in cell 2 choose `MODE = "target"` from the drop-down, or edit the line. Then run **Runtime → Run all**. The zip is named `eaios-m1-gpu-target-<run id>.zip`. To switch modes, always use **Run all**: cell 1 then starts a new run ID. Cell 2 refuses to mix modes within one run.

## What is recorded

* **Environment (cell 1):** GPU name, compute capability, driver and CUDA version, and GPU, host and disk memory, all recorded before anything is installed.
* **Software:** the pinned vLLM environment in its own uv venv: what was installed before, the action taken (`create`, `reuse` or `repair`), the installed versions afterwards, and the full package freeze. Plus the kernel's torch and transformers versions.
* **Tokenizer preflight:** the tokenizer class, vocabulary size and special-token count as seen through vLLM, recorded before vLLM starts.
* **Models:** for each downloaded model, the repository, requested revision, resolved commit SHA, files, bytes, download seconds and license.
* **Stages:** status, duration, details, and any error with its traceback. `failures[]` lists every failure, with OOMs classified separately.
* **Memory:** NVML samples every 100 ms give peak GPU use, minimum headroom, the kernel's torch allocator peaks and peak host RAM, overall and for each stage.
* **Timings:** for each step, time to first token, total seconds, token counts, decode tokens per second, and checker latency per claim and per answer sentence. Target mode also records concurrent throughput.

**Target mode verdicts.** Each configuration is classified as *passed*, *failed* or *incomplete*. Passing requires every one of these stages:

* vLLM startup
* co-resident loading of BGE-M3 and the checker
* warm-up
* corpus embedding
* every question pass
* concurrent load
* cancellation
* restart, which runs for **every** configuration

Any missing or skipped stage makes the configuration *incomplete*. Only a passed configuration can be suggested as the candidate, which is the one with the most measured headroom. Choosing the configuration remains a project decision.

**Cancellation evidence.** The client must close a stream that is still unfinished (`client_closed_early: true`, with the chunk count recorded). After that, `vllm:num_requests_running` must return to 0 and a follow-up request must succeed. If the model finishes before the cut-off, the stage fails with the reason "nothing was cancelled".

## Limits of this evidence

* **Checker:** the checker is a public zero-shot NLI checkpoint used as a **development baseline**, not the fine-tuned checker from M7. Its agreement with the expected labels is a smoke count, not an accuracy result. The 0.5 threshold is uncalibrated.
* **Workload:** `m1-gpu-workload-v1` (ten questions) was accepted on 2026-11-20 for repeatable timing. It is kept separate from held-out quality evaluation. Its SHA-256 is recorded with each run.
* **Fixed evidence:** generation uses fixed evidence (the frozen Q1 2026 figures and gold passages). The generated SQL is recorded but not executed. Retrieval runs only as a smoke ranking over ten passages.
* **Network:** vLLM listens on `127.0.0.1` with a random per-run API key that is never saved. No tunnel is opened.
* **Model revisions:** each model resolves to a commit SHA at run time. To repeat a run exactly, set the recorded SHAs in cell 2.

## If a stage fails

The failure is recorded in `results.json` and in the vLLM log inside the zip, and the run continues where it can. Return the zip either way. To retry with changed settings, edit cell 2 and run **Run all** again:

* `vllm.enforce_eager: True`, if CUDA-graph capture fails or runs out of memory
* `vllm.attention_backend: "TRITON_ATTN"` or `"FLEX_ATTENTION"`, if the default attention backend is rejected on the T4
* `vllm-env-pins.txt` (in the repository), to test another vLLM and Transformers pair. Change it there, regenerate the notebook, and the CPU tokenizer test checks the new pair.

## Editing and tests

The notebook is generated, so don't edit it directly:

* **Benchmark logic:** edit `build_notebook.py`. Pure logic lives in `bench_core.py`, which is inlined verbatim into cell 7.
* **Regenerate the notebook:**

  ```bash
  python ml/gpu-feasibility/build_notebook.py
  ```

* **Run the tests:** these are CPU-only and use the standard library only. The two vLLM tokenizer tests are skipped unless `EAIOS_VLLM_PYTHON` is set (see below).

  ```bash
  python -m unittest discover -s ml/gpu-feasibility/tests -v
  ```

  They cover:

  * the reuse, repair and create decisions for the vLLM venv, plus the real cell 4 run against a simulated venv holding the old `vllm 0.11.0` + `transformers 5.23.0` pair
  * natural early completion versus client interruption of a mocked stream
  * failed, skipped and untested restarts
  * candidate selection
  * a check that the notebook matches its generator
  * the default mode
  * downloading only the models for the selected mode

* **Run the real vLLM tokenizer test on CPU:** this installs the pinned environment with CPU torch, downloads only the tokenizer files of Qwen3-1.7B and Qwen3-8B-AWQ at pinned revisions, then initialises both tokenizers through vLLM's `get_tokenizer()`.

  ```bash
  uv venv --python 3.12 /tmp/vllm-env
  ```

  ```bash
  uv pip install --python /tmp/vllm-env/bin/python --torch-backend cpu -r ml/gpu-feasibility/vllm-env-pins.txt
  ```

  ```bash
  EAIOS_VLLM_PYTHON=/tmp/vllm-env/bin/python python -m unittest ml/gpu-feasibility/tests/test_vllm_tokenizer.py -v
  ```

CI runs `build_notebook.py --check` and the stdlib tests in the `gpu-feasibility-notebook` job. The `vllm-tokenizer` job runs the real tokenizer test as required, not skipped.
