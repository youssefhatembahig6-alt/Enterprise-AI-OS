# M2 model gateway (Colab GPU)

The backend talks to models through one gateway root URL (`LLM_BASE_URL`). This directory contains that gateway for the Colab T4 runtime. Inference never runs on the laptop.

| Route | Backed by | Used by M2 |
| --- | --- | --- |
| `GET /health` | served models, pinned revisions, engine settings | yes: the backend refuses to run unless the chat model and revision match, and refuses a different checker |
| `POST /v1/chat/completions` | local vLLM, streamed relay | yes: router fallback, SQL drafts, report sentences |
| `POST /v1/embeddings` | BGE-M3 dense | no (M3); separate route so it is never conflated with chat |
| `POST /check` | DeBERTa-v3-base-mnli-fever-anli | yes: narrative claim scores (development baseline, not the M7 checker) |

Every route except `/health` requires `Authorization: Bearer $GATEWAY_TOKEN`. Closing a chat stream closes the upstream vLLM stream, which aborts the request (measured in M1). The checker refuses inputs longer than 512 tokens instead of truncating them; the backend then withholds the narrative and publishes only the deterministically verified table.

## Measured configuration

Pinned in [`gateway.py`](gateway.py) and in the backend settings (`MODEL_*_REVISION`), from the [M1 T4 report](../gpu-feasibility/reports/2026-11-20-target-t4.md):

| Setting | Value |
| --- | --- |
| Chat | `Qwen/Qwen3-8B-AWQ` @ `4da05a8edb55c6046cce958586c33b61da07bb79`, `--quantization awq --dtype half` |
| Context and batch | `--max-model-len 4096 --max-num-seqs 8` |
| GPU memory | `--gpu-memory-utilization 0.75` (0.85 ran out of memory under concurrent load) |
| Embedder | `BAAI/bge-m3` @ `5617a9f61b028005a4858fdac845db406aefb181` (fp16) |
| Checker | `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli` @ `6f5cf0a2b59cabb106aca4c287eed12e357e90eb` (fp16), threshold 0.5, uncalibrated |
| vLLM environment | `ml/gpu-feasibility/vllm-env-pins.txt` (vllm 0.11.0, transformers 4.56.2) |

## Running it (manual, owner-operated)

1. Open a Colab T4 runtime and install the pinned vLLM environment into `/content/vllm-env`, exactly as cells 1–5 of the [M1 notebook](../gpu-feasibility/m1_gpu_feasibility.ipynb) do.
2. In the kernel: `pip install fastapi==0.116.1 uvicorn==0.35.0 httpx==0.28.1`, clone this repository to `/content/Enterprise-AI-OS`, then:

   ```python
   import os, secrets
   os.environ["GATEWAY_TOKEN"] = secrets.token_urlsafe(32)
   print(os.environ["GATEWAY_TOKEN"])  # copy once into the backend .env, then clear the output
   %run /content/Enterprise-AI-OS/ml/model-gateway/colab_launch.py
   ```

3. Expose `127.0.0.1:8100` through a tunnel (the specification names ngrok). **Usage gate:** Google's Colab FAQ restricts remote proxies and unrelated web services on managed runtimes. Confirm that this interactive, short-lived demonstration is acceptable before opening a tunnel; if it is not, record that as a blocker rather than working around it (planning roadmap, "Colab feasibility has two gates").
4. On the laptop, set in `.env`: `MODEL_MODE=gateway`, `LLM_BASE_URL=https://<tunnel-host>` (the root, no `/v1`) and `MODEL_GATEWAY_TOKEN=<token>`. Restart the API and worker.
5. Capture the demonstration: `python3 scripts/m2_demo_capture.py` (see [docs/runbook.md](../../docs/runbook.md)).

## M3: embeddings and the retrieval measurement

`POST /v1/embeddings` with `"return_sparse": true` returns BGE-M3's dense vector and its sparse lexical weights (`{"indices", "values"}`), using the M1 notebook's sparse head. The backend uses them for ingestion and for dense or hybrid (RRF) retrieval.

To produce the real retrieval comparison without a tunnel, run Qdrant in the same Colab runtime and the harness over loopback, as the M2 demonstration did with the API:

```bash
# in Colab, after colab_launch.py is serving on 127.0.0.1:8100
wget -q https://github.com/qdrant/qdrant/releases/download/v1.15.4/qdrant-x86_64-unknown-linux-gnu.tar.gz
tar xzf qdrant-x86_64-unknown-linux-gnu.tar.gz && (./qdrant >/content/qdrant.log 2>&1 &)
cd /content/Enterprise-AI-OS/backend && pip install -r requirements.lock --no-deps -q
MODEL_MODE=gateway LLM_BASE_URL=http://127.0.0.1:8100 MODEL_GATEWAY_TOKEN=$GATEWAY_TOKEN \
  python -m app.cli retrieval-eval --qdrant http://127.0.0.1:6333 --out /content/retrieval-eval.json
```

These Colab steps have not been exercised yet; Colab's Python version may need the backend dependencies installed without `--require-hashes`. The JSON records the embedding space and revisions, Qdrant version, corpus and case hashes, per-case ranks, and recall@1/@5, MRR@5 and document recall@5 for dense and hybrid.

## Tests

`backend/tests/unit/test_model_gateway_contract.py` loads this exact file with stub models on CPU and drives it with the backend's `GatewayModelClient`: route composition, auth, streaming relay, checker refusal of over-long input, and the vLLM command flags.
