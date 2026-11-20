# ADR 0105 — Runtime, pinned versions and Compose

Status: accepted for M0, pending planning-agent review. Source: [specification](https://app.notion.com/p/Enterprise-AI-OS-Project-Documentation-41e095eca45681919bddcf0227a8699f) §7 "Docker Compose services", "Mixed operating systems"; ADRs 0007, 0008, 0013 (Decided); NFR-03, NFR-04.

## Decision

- One backend image runs `api` (uvicorn/FastAPI), `worker` (Celery, `--concurrency=2`), `scheduler` (Celery beat) and the one-shot `migrate`/`seed-dev` commands. The frontend is built by Vite and served by nginx on 127.0.0.1:3000.
- Only `api` (127.0.0.1:8000) and `frontend` (127.0.0.1:3000) are published. PostgreSQL, Redis (password-protected), Qdrant and SeaweedFS ("storage", S3 on 8333) are internal.
- Every image tag is pinned to an exact version (never `latest`). Python dependencies install from a hash-locked `requirements.lock`; npm from `package-lock.json`. See [docs/contracts/runtime.md](../contracts/runtime.md).
- **External model boundary.** No model runs in Compose or on laptops. `LLM_BASE_URL` is optional in M0. When it is set, `/ready` reports `model_inference: external_unchecked`, otherwise `not_configured`; it never reports model capability as available, and the model endpoint does not affect readiness.
- Liveness `/health` touches no dependency. Readiness `/ready` checks the PostgreSQL schema head (via the app role), Redis PING, Qdrant `/readyz` and the S3 endpoint, and returns `503` with component names only.
- Commands are Python/npm scripts (`scripts/dev.py`), not shell scripts or a Makefile; `.gitattributes` forces LF. The only shell script is the PostgreSQL container's first-run init hook, which runs inside the Linux container.

## Consequences

The SeaweedFS S3 endpoint runs without S3 identities in M0. It is reachable only on the internal network, and no buckets are created. S3 credentials and private buckets are part of the first upload milestone (M3).
