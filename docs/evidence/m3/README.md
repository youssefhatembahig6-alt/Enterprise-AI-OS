# M3 real-model acceptance evidence

| Artifact | What it records |
| --- | --- |
| [Document answer and citation](2026-11-21-colab-demo.json) | Gateway-mode SSE for the finance and sales readers, plus the opened citation |
| [Retrieval comparison](2026-11-21-colab-retrieval-eval.md) · [JSON](2026-11-21-colab-retrieval-eval.json) | Dense and hybrid results, per-case ranks, and the lexical gate on a fresh BGE-M3 index |
| [Relevance study](2026-11-21-colab-relevance-study.md) · [JSON](2026-11-21-colab-relevance-study.json) | Lexical errors and a dense cosine threshold sweep on 24 diagnostic pairs |
| [Docling scores](2026-11-21-colab-parsers-scores.md) · [JSON](2026-11-21-colab-parsers-scores.json) | Application output scored against the M1 references |
| [Fallback scores](2026-11-21-colab-parsers-after-fallback-scores.md) · [JSON](2026-11-21-colab-parsers-after-fallback-scores.json) | The same checks with the gateway parse route unavailable |
| [Versions and settings](2026-11-21-colab-versions.json) | Clean application SHA, gateway health, service versions and runtime settings |
| [`2026-11-21-compose-fake-demo.json`](2026-11-21-compose-fake-demo.json) | `scripts/m3_demo.py capture` on the laptop Compose stack (API, worker, scheduler, PostgreSQL, Qdrant, SeaweedFS). It holds one authorized answer as `niletech/finance-reader`, with its opened citation (Q1 2026 finance commentary, version 1, code points 401–509, integrity `verified`), and the `no_sources` response for `niletech/sales-reader` (events `status` ×3, `done`). **Inference is FAKE** (labelled on every event): it is integration evidence for the pipeline, authorization and citations, **not** model evidence. |

## Run configuration

The answer capture, measurements and fallback used `dd585fbeeb39e2fb6875b7a9d2f0ac72d4ef6d9f` on a Colab Tesla T4. The API, gateway, PostgreSQL 17.12 from PGDG, Redis 7.0.15 and Qdrant 1.15.4 communicated over loopback. The API used inline dispatch and filesystem storage. The separate Compose test stack exercised PostgreSQL 17.6, Redis 7.4.5, Celery and SeaweedFS.

| Component | Version or revision |
| --- | --- |
| Qwen/Qwen3-8B-AWQ | `4da05a8edb55c6046cce958586c33b61da07bb79`, vLLM 0.11.0, AWQ, fp16, memory utilization 0.75, context 4096 |
| BAAI/bge-m3 | `5617a9f61b028005a4858fdac845db406aefb181`, dense and sparse vectors |
| MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli | `6f5cf0a2b59cabb106aca4c287eed12e357e90eb`, development baseline, uncalibrated threshold 0.5 |
| Docling | 2.151.1, docling-core 2.108.0, docling-ibm-models 4.0.4, default layout, table and OCR pipeline on CPU |

The run started at `85527f0`. Two concurrent ingestion jobs attempted to create the same Qdrant collection. One received HTTP 409 and left a queued job. After applying `dd585fb`, which re-reads the collection on conflict, the standard ingestion worker retried the queued job successfully. The gateway and previously completed document versions stayed in the runtime. The API was restarted at `dd585fb` before the capture and the evaluation created its own fresh index.

## Acceptance checks

- Finance received a published answer in 77.9 s with `answer_quotes_verified_dev_checked`. Qwen answered **370 open invoices**. Citation `c1` opened *Q1 2026 finance commentary* v1 with `integrity=verified`. Independently parsing the fixture reproduced the canonical text hash and exactly matched the quote at code points [401, 509).
- Sales received `no_sources`. The stream consisted of three status events and one `done` event with an abstention. The payloads contained no document content.
- Docling matched M1 at 100% reading order on the two-column PDF and 100% table-cell recovery on the table-heavy PDF. The latter scored 73.3% on reading order versus 100% in M1. The scan was `ready` and matched M1 on text similarity (97.2%), word recall (66.2%), reading order (91.7%) and cells (80.0%).
- With the parse route unavailable, the text-layer PDFs used `pdfplumber-text-v1` and recorded `unavailable: GatewayUnavailable`. The scan was `needs_ocr` and produced no canonical text.
- Colab's scanner checked the bundle for generated credentials. The ten files above were recovered byte-for-byte from the notebook output after the browser download failed. The recovered set matched the Colab transfer SHA-256 `4fb1b2ad1bef142fccb482ae57f7bb76dd0972f23aa2e2b59d3ef2a89171c2c3`. The demo file's SHA-256 is `93833dad87ff19c325bff845018782e0145157d11c1b1d959dcf85560544704c`.

## Retrieval and relevance decisions

Both modes used the same fresh index of 94 chunks and 22 frozen development questions.

| Mode | Recall@1 | Recall@5 | MRR@5 | Document recall@5 |
| --- | ---: | ---: | ---: | ---: |
| Dense | 0.864 | 0.909 | 0.886 | 1.000 |
| Dense + sparse (RRF) | 0.773 | 0.909 | 0.841 | 0.955 |

Keep dense. Hybrid shifted `fin-travel` and `it-password-length` from rank 1 to 2. Both missed the exact passages for `pdf-feb-net` and `pdf-pending` in the top five.

Keep `RELEVANCE_FILTER=lexical` for M3. It retained all 20 retrieved gold hits in each mode and kept 28/51 other-document hits in dense mode and 36/54 in hybrid. Other-document hits are a proxy for irrelevant context. In the 24 diagnostic pairs the gate dropped eight relevant paraphrases and kept nine irrelevant pairs. A dense threshold of 0.5298 kept every relevant study pair and three irrelevant pairs. The same threshold dropped two retrieved gold hits in the corpus. A threshold of 0.5396 made three study errors and dropped three corpus gold hits. The study deliberately exercises failure modes, and the counts apply to these pairs. The answer still goes through quote and entailment checks. [ADR 0304](../../adr/0304-citations-and-refusals.md)

## Reproduction

Use the T4 notebook and loopback setup in the [Colab runbook](../../../ml/colab-m3/README.md). At the application SHA above, run the notebook through its gateway cell and run the following in that runtime.

```bash
python ml/colab-m3/run_m3_colab.py all
```

The runner drives `scripts/m3_demo.py seed` and `capture`, then `python -m app.cli retrieval-eval --docling`, `python -m app.cli relevance-study`, and `python -m app.cli parser-export` followed by `python -m bench.score_app`. It collects version metadata and repeats the parser scoring with the parse route unavailable. The frozen corpus is synthetic. The demo covers one question and the comparisons cover development fixtures.
