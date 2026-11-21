# M3 acceptance run (Colab T4, loopback only)

One Colab runtime runs the model gateway (Qwen3-8B-AWQ on vLLM, BGE-M3, the development checker and Docling) and the application next to it: PostgreSQL 17, Redis, Qdrant v1.15.4 and the API, all on `127.0.0.1`. No tunnel is opened, as in the M2 demonstration ([docs/evidence/m2](../../docs/evidence/m2/README.md)).

[`run_m3_colab.py`](run_m3_colab.py) produces every M3 acceptance artifact in one pass:

| Output | Command it runs | What it shows |
| --- | --- | --- |
| `m3-demo-colab.json` | `scripts/m3_demo.py seed --timeout 3600`, then `capture` | Finance reader gets a published, cited Qwen answer; the citation opens with verified integrity; the sales reader gets `no_sources` |
| `retrieval-eval.json`, `.md` | `python -m app.cli retrieval-eval --docling` | Dense versus dense + sparse (RRF) on the same BGE-M3 index and 22 cases: recall@5, MRR@5, per-case rank differences, the lexical gate on every top-5 hit |
| `relevance-study.json`, `.md` | `python -m app.cli relevance-study` | Lexical gate and BGE-M3 cosine on 24 labelled pairs (paraphrases without shared terms, keyword traps, controls) |
| `parsers-scores.json`, `.md` | `python -m app.cli parser-export` + `bench.score_app` | The PDFs as ingested by the application (Docling via the gateway) scored with the M1 scorer against the M1 gold, beside M1's Docling scores |
| `parsers-after-fallback-scores.*` | second API with the parse route unreachable | The documented fallback: text-layer parser, `parse_notes.docling = unavailable: …`, scan stays `needs_ocr` |
| `versions.json` | — | Application commit, gateway `/health` (models and revisions, Docling version), PostgreSQL, Redis, Qdrant, Python, Docling freeze hash, GPU, settings |

## Cells

1. **Runtime:** T4 GPU. Install the pinned vLLM environment into `/content/vllm-env` exactly as cells 1–5 of the [M1 GPU notebook](../gpu-feasibility/m1_gpu_feasibility.ipynb) do, and clone this repository at the PR head:

   ```python
   !git clone -b feature/m3-authorized-document-answers https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS /content/Enterprise-AI-OS
   !pip -q install fastapi==0.116.1 uvicorn==0.35.0 httpx==0.28.1
   ```

2. **Docling environment** (M1 pins, Python 3.12, separate venv because its pins conflict with vLLM's):

   ```python
   !python /content/Enterprise-AI-OS/ml/colab-m3/run_m3_colab.py docling-env
   ```

3. **Gateway** with Docling enabled:

   ```python
   import os, secrets
   os.environ["GATEWAY_TOKEN"] = secrets.token_urlsafe(32)   # stays in this runtime
   os.environ["DOCLING_PYTHON"] = "/content/venv-docling/bin/python"
   %run /content/Enterprise-AI-OS/ml/model-gateway/colab_launch.py
   ```

4. **Everything else** (about 20–40 minutes; Docling runs on the CPU so it does not compete with vLLM for the T4):

   ```python
   !python /content/Enterprise-AI-OS/ml/colab-m3/run_m3_colab.py all
   ```

   Set `RETRIEVAL_MODE` / `RELEVANCE_FILTER` / `RELEVANCE_MIN_SCORE` in `os.environ` before this cell to run the demo with other settings.

5. **Download** `/content/m3-evidence-<stamp>.zip`. The runner refuses to write it if any generated password, the JWT secret or the gateway token appears in an output.

## Notes

- Random database, Redis and JWT secrets and the seeded user passwords live only in `/content/m3-run/secrets` (mode 0700) and are never bundled.
- Dispatch is inline (`REPORT_DISPATCH=inline`), the document store is the local filesystem, and PostgreSQL comes from PGDG packages, as in M2. Celery and SeaweedFS are covered by the Compose stack and the integration tests.
- The fallback documents are uploaded without permissions (`permission_state = unknown`), so they can never be retrieved, and only after the demo and the measurements.
