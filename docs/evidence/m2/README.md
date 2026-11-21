# M2 real-model acceptance evidence

| Item | Value |
| --- | --- |
| Capture | [`2026-11-21-colab-real-model.json`](2026-11-21-colab-real-model.json), written by `scripts/m2_demo_capture.py` |
| SHA-256 | `192eb9759fe1b5ff76980446a24c8e25df815b4e7fe90a7af03ae537eac4d490` (byte-identical to the owner's capture; `docs/evidence/** -text` prevents normalization) |
| Application commit | `1f1c127bf66bda476a92091e2f693d1cc181a6ff` (PR #5 head at capture time) |
| Captured | 2026-11-21 03:00:59 UTC, run `727486d0-c979-4b2a-87c3-2a63718b1369` |
| Question | “What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?” as `niletech/finance-reader` |

## How the demonstration ran

- **Where:** one Colab runtime with a Tesla T4. The API, PostgreSQL (`postgres (PostgreSQL) 17.12 (Ubuntu 17.12-1.pgdg24.04+1)`) and the model gateway (`ml/model-gateway/`) ran together in that runtime and talked over **loopback only** (API at `http://127.0.0.1:18000`). No tunnel was used, so the Colab remote-proxy question did not arise for this run.
- **Dispatch:** `REPORT_DISPATCH=inline`, i.e. the durable `report_jobs` row and the same `ReportRunner`, run on the API's thread pool instead of Celery.
- **Model:** real inference through the gateway (`inference_mode = gateway` on every event and on the run). The served configuration was `Qwen/Qwen3-8B-AWQ@4da05a8edb55c6046cce958586c33b61da07bb79`, vLLM 0.11.0, AWQ, fp16, `gpu_memory_utilization 0.75`, `max_model_len 4096`, `max_num_seqs 8`, seed 0. The checker was `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2b59cabb106aca4c287eed12e357e90eb`, a **development baseline, not the final trained checker**, with an uncalibrated threshold of 0.5. The backend's pinned-revision check passed, so it accepted the served models.
- **Separately, on the laptop:** the Compose stack ran the same commit through **Celery** with the labelled **stand-in (fake) model**. That run proves the worker/outbox path. It is not model evidence, and this capture does not depend on it.

## What the capture shows (checked 2026-11-21)

| Criterion | Recorded |
| --- | --- |
| Real Qwen inference | Two model calls: `sql` (88 tokens, 53.4 s, TTFT 28.2 s) and `report` (147 tokens, 44.7 s); no `FAKE` label anywhere |
| Published report | `done.outcome = published`, run status `published`, exactly one `done` (id 19, last). Only `status` events came before the `publication` status |
| Verification label | `figures_verified_narrative_dev_checked`; narrative `verified`; checker scores 0.9929 / 0.9972 / 0.9899 |
| Expected Q1 2026 figures | 291 fulfilled orders; net **USD 80,934,385.00**; tax **USD 11,330,813.90**; gross **USD 92,265,198.90**. Net + tax = gross exactly. These match the frozen case `m1-sales-q1-2026-net-fulfilled` |
| Verified evidence | Evidence `64cdb081-19e8-4485-9062-5ff91ef5a248`, integrity `verified`. Its `result_sha256` `1807dc30…29ab` was **independently recomputed** from the captured fields plus the deterministic NileTech fixture tenant id, and it matches |
| Query meaning | Order-date window 2026-01-01..2026-03-31, `FULFILLED` only, USD, period total. Join probe: 291 joined rows = 291 distinct orders (no fan-out). Dataset `niletech-0.1.6:e5715a94…f127` |
| Repairs | None needed (1 SQL attempt, 1 report attempt, no rejections) |
| Stage timings | First status at 0.042 s; result published at 99.4 s. `generate_sql` 53.4 s, `draft_report` 44.7 s, `verify_report` 1.1 s, `execute` 0.06 s |
| Credentials | None. Inspected manually and by pattern scan: no tokens, passwords, keys, URLs with credentials or tunnel hosts. Long hex strings are public commit, model, dataset and result hashes |

## Deviations from the Compose configuration

- PostgreSQL 17.12 (Ubuntu packages), not the pinned `postgres:17.6-bookworm` image. The migrations, roles, RLS and query path were the same code.
- Inline dispatch, not Celery. Celery dispatch is covered by the laptop run and the integration tests (`test_outbox_redispatches_a_lost_job`, `test_idempotent_create_and_duplicate_job_delivery`).
- This is one run on one question. It shows the pipeline works end to end with the real model; it is not a quality evaluation (M7).
