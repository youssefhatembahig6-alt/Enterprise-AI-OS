# Runbook — local development stack (M0–M2)

Prerequisites: Docker Engine with Compose v2, and Python 3 for the helper script. Node 20.19+ is needed only for local frontend work. No GPU, model endpoint or paid API key is needed.

On Debian, if your login session is not yet in the `docker` group, wrap commands as `sg docker -c "<command>"`.

## First start (clean machine)

```bash
python3 scripts/dev.py init-env      # writes .env with random secrets (0600); never overwrites
python3 scripts/dev.py up            # docker compose up --build -d --wait
python3 scripts/dev.py seed          # explicit development seed (idempotent)
```

- `up` starts PostgreSQL, Redis, Qdrant and SeaweedFS. The one-shot `migrate` service applies Alembic migrations as the owner role. Then the API, worker, scheduler and frontend start.
- The PostgreSQL first-run hook (`infra/postgres/init-roles.sh`) creates `eaios_owner`, `eaios_app` and the `eaios` database only when the data volume is empty.
- Open http://localhost:3000. API: http://localhost:8000 (`/health`, `/ready`, `/docs`).

## Development login credentials

`seed` generates a random password for each fixture user it **creates**. It writes them to `var/dev-credentials.json` (git-ignored, mode 0600) and does not print them. Read that file locally, for example:

```bash
python3 -c "import json; print(json.load(open('var/dev-credentials.json'))['niletech']['finance.reader@niletech.example'])"
```

Tenant slugs: `niletech` and `isolation-test`. The users are listed in `backend/fixtures/foundation.toml`. If the file is lost, regenerate the fixture passwords (development only):

```bash
docker compose run --rm --user "$(id -u):$(id -g)" -v "$PWD/var:/app/var" migrate python -m app.cli seed-dev --reset-passwords --credentials-file /app/var/dev-credentials.json
```

**If the seed is interrupted after the database commit.** The database transaction commits first, then the new passwords are written to the credentials file. That file is replaced atomically, using an owner-only temporary file in the same directory followed by `os.replace`, so an interruption keeps the previous valid file. Newly created users would then have hashes in the database but no saved password. Run the `--reset-passwords` command above to issue and save fresh passwords for every fixture user. A reseed that creates no users never touches the file.

Seeding refuses unless `EAIOS_ENV` is `development` (or `test`, for the test suite), and it never runs on startup.

## NileTech business data (M1)

The NileTech sales baseline (`niletech-0.1.6`) is imported explicitly, after the M0 seed. It is never imported on startup and is refused unless `EAIOS_ENV` is `development` or `test`.

```bash
python3 scripts/dev.py business-data-report   # verify source, print fingerprint/counts/anomalies (read-only)
python3 scripts/dev.py import-business-data   # verify, then load into tenant "niletech" (owner role)
```

- Both commands use the vendored subset in `backend/fixtures/business/niletech-0.1.6`, after checking every hash against the original source manifest.
- To verify against the original archive instead, run `python -m app.cli business-data-report --zip ../reference-data/niletech-0.1.6.zip` from `backend/`. That checks all 103 archive entries.
- Re-running the import inserts nothing. It fails without changing anything if the stored rows drift from the source, or if the tenant already holds a different dataset.
- Provenance, ID mapping, the reporting definition and the known source anomalies are in `backend/fixtures/business/niletech-0.1.6/README.md`. The worked Q1 2026 example is in `docs/data/sales-report-q1-2026.md`.

## Verified SQL reports (M2)

**New volume:** `init-env` now generates `EAIOS_REPORT_DB_PASSWORD`, and the first-run hook creates the read-only report role. **Existing volume (created before M2):** run once, before `up`:

```bash
python3 scripts/dev.py add-report-role   # adds EAIOS_REPORT_DB_PASSWORD to .env if missing; creates/updates eaios_report
python3 scripts/dev.py up                # migrate applies 0003_verified_reports and 0004_report_session_identity
python3 scripts/dev.py seed              # inserts only the new M2 fixture rows (role, dataset resource, grants, profile)
python3 scripts/dev.py import-business-data   # if not already imported
```

Then sign in at http://localhost:3000 as `finance.reader@niletech.example` (tenant `niletech`) and ask, for example, “What were net (tax-exclusive) fulfilled sales for Q1 2026, in USD?”.

| Model mode | How | Result |
| --- | --- | --- |
| No gateway (default) | `MODEL_MODE=gateway`, `LLM_BASE_URL` empty | Runs end with “The model service is unavailable” (`model_unavailable`) |
| Labelled fake (development only) | `MODEL_MODE=fake docker compose up -d --wait api worker` | Deterministic double; the UI, every event and the run record say **FAKE INFERENCE**. Not model evidence |
| Real model | Start the Colab gateway ([ml/model-gateway/README.md](../ml/model-gateway/README.md)); set `MODEL_MODE=gateway`, `LLM_BASE_URL=https://<tunnel-host>` (root, no `/v1`), `MODEL_GATEWAY_TOKEN`; `docker compose up -d --wait api worker` | Pinned Qwen3-8B-AWQ at utilization 0.75 / context 4096; the backend refuses other revisions |

Capture a run (raw SSE frames with arrival times, run diagnostics, evidence) to `var/m2-demo-<timestamp>.json`:

```bash
python3 scripts/m2_demo_capture.py
```

The script prints whether the server labelled the run `gateway` or `fake`; only a `gateway` run with served revisions in its diagnostics is real-model evidence.

## Restart

```bash
python3 scripts/dev.py down    # stops containers; named volumes are kept
python3 scripts/dev.py up      # data, users and sessions persist; migrations are a no-op at head
```

## Checks

```bash
python3 scripts/dev.py check          # Ruff, Mypy, unit tests, ESLint, tsc, Vite build, compose config
python3 scripts/dev.py test-backend   # full backend suite in Compose against real PostgreSQL/Redis (creates its own report role)
python3 scripts/smoke_stack.py        # end-to-end checks against the running stack
```

Local backend tooling: `cd backend && python3 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.lock`. To run integration tests from the host, point `TEST_DATABASE_ADMIN_URL` and `TEST_REDIS_URL` at disposable services; each run creates and drops its own database and roles.

## Destructive reset (separately labelled)

```bash
python3 scripts/dev.py DESTROY-LOCAL-DATA --yes-delete-my-local-eaios-data
```

This deletes this project's containers **and named volumes**: all local tenants, users, sessions and audit events. It refuses without the confirmation flag. Never run it against a database you want to keep. Tests never touch the development database; they create a disposable one.
