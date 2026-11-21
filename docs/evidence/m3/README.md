# M3 evidence

| File | What it is |
| --- | --- |
| [`2026-11-21-compose-fake-demo.json`](2026-11-21-compose-fake-demo.json) | `scripts/m3_demo.py capture` on the laptop Compose stack (API, worker, scheduler, PostgreSQL, Qdrant, SeaweedFS). It holds one authorized answer as `niletech/finance-reader`, with its opened citation (Q1 2026 finance commentary, version 1, code points 401–509, integrity `verified`), and the `no_sources` response for `niletech/sales-reader` (events `status` ×3, `done`). **Inference is FAKE** (labelled on every event): it is integration evidence for the pipeline, authorization and citations, **not** model evidence. |

Still to capture with the real model (Colab gateway): the same demonstration in gateway mode, and the BGE-M3 retrieval comparison (`python -m app.cli retrieval-eval`, see `ml/model-gateway/README.md`).
