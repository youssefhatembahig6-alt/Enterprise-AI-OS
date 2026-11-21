# Runtime contract (M0)

See also [ADR 0105](../adr/0105-runtime-and-compose.md) and the [runbook](../runbook.md).

## Pinned versions

Recorded 2026-11-20. Image digests are the ones pulled for the local acceptance run. Build base images were resolved by BuildKit and pinned by tag.

| Component | Version | Notes |
| --- | --- | --- |
| PostgreSQL | `postgres:17.6-bookworm` | `sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3` |
| Redis | `redis:7.4.5-alpine` | `sha256:bb186d083732f669da90be8b0f975a37812b15e913465bb14d845db72a4e3e08` |
| Qdrant | `qdrant/qdrant:v1.15.4` | `sha256:6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1` |
| SeaweedFS | `chrislusf/seaweedfs:3.96` | `sha256:a35a92243aefd043b974ec3ebcc0e9511efb49894339f6c1ece087b44eb52b3b` |
| Backend base | `python:3.13.7-slim-bookworm` | Python 3.13 also matches Debian 13 hosts |
| Frontend build | `node:22.19.0-alpine3.22` | local development also verified on Node 20.19.2 |
| Frontend serve | `nginx:1.28.0-alpine` | |
| FastAPI / Starlette | 0.116.1 / 0.47.3 | full hashed set in `backend/requirements.lock` |
| SQLAlchemy / Alembic | 2.0.43 / 1.16.5 | |
| psycopg (binary) | 3.2.10 | |
| Pydantic / pydantic-settings | 2.11.9 / 2.10.1 | |
| Celery / kombu / redis-py | 5.5.3 / 5.5.4 / 5.2.1 | |
| PyJWT / argon2-cffi | 2.10.1 / 25.1.0 | |
| pytest / Ruff / Mypy | 8.4.2 / 0.13.0 / 1.18.1 | `backend/requirements-dev.lock` |
| React / React DOM | 19.1.1 | `package-lock.json` |
| Vite / plugin-react | 7.1.5 / 5.0.2 | Vite 7 requires Node ≥ 20.19 |
| Tailwind CSS (+ Vite plugin) | 4.1.13 | |
| TypeScript / ESLint / typescript-eslint | 5.9.2 / 9.35.0 / 8.43.0 | npm reports ESLint 9.35 as out of support; upgrade is a follow-up |
| LangGraph (M2) | 1.2.19 | released 2026-10-30; pulls langchain-core 1.8.4 and langsmith 0.15.1 (tracing stays off unless LANGSMITH_TRACING is set) |
| pglast (M2) | 7.22 | PostgreSQL 17 parser (libpg_query), released 2026-10-31 |

Lockfiles: `backend/requirements.lock` and `backend/requirements-dev.lock` (pip-compile 7.5.0 with `--generate-hashes`; install with `pip install --require-hashes`), and `package-lock.json` (npm workspaces). Regenerate with `python scripts/dev.py lock` and `npm install`.

## Services and memory limits

| Service | Purpose | Published | `mem_limit` |
| --- | --- | --- | --- |
| postgres | records, permissions, audit | no | 512m |
| redis | Celery broker, login rate limits | no | 128m (`maxmemory 96mb`, `noeviction`) |
| qdrant | vectors (unused until M3) | no | 512m |
| storage | SeaweedFS, S3 on 8333 (unused until M3) | no | 256m |
| migrate | one-shot Alembic (owner role) | no | 256m |
| api | FastAPI (one uvicorn process; at most 2 concurrent Argon2 verifications ≈ 196 MiB peak, measured) | 127.0.0.1:8000 | 384m |
| worker | Celery, concurrency 2 | no | 384m |
| scheduler | Celery beat (hourly session purge) | no | 160m |
| frontend | nginx serving the Vite build | 127.0.0.1:3000 | 64m |

The steady total is about 2.4 GB, or 2.6 GB while `migrate` runs. The optional `backend-tests` profile adds 512m.

## External model boundary

No model service runs in this stack, and no GPU or paid API key is needed to start it. M2 reports call the external gateway (`LLM_BASE_URL` root + explicit paths, bearer `MODEL_GATEWAY_TOKEN`; [model-gateway.md](model-gateway.md), `ml/model-gateway/`). Without it, runs end with `model_unavailable`. `MODEL_MODE=fake` selects a labelled deterministic double for development and tests. `/ready` reports `model_inference` as `not_configured`, `external_unchecked` or `fake`, never as available.

## Report query role (M2)

`eaios_report` (password `EAIOS_REPORT_DB_PASSWORD`) is created by `infra/postgres/init-roles.sh` on a new volume, or by `python3 scripts/dev.py add-report-role` on an existing one, from `infra/postgres/report-role.sql`. The API and worker connect with it through `REPORT_DATABASE_URL`; migrations grant it only the `reporting` views ([ADR 0201](../adr/0201-restricted-report-sql.md)). Celery runs report jobs (`app.tasks.reports.run_report`, 1200 s hard limit) and beat re-dispatches pending jobs every 30 s.

## Start, restart and reset

See the [runbook](../runbook.md). In short: `init-env` → `up` → `seed`. `down`/`up` keeps the named volumes. Re-seeding inserts nothing. Only `DESTROY-LOCAL-DATA --yes-delete-my-local-eaios-data` removes volumes.
