# Runbook — local development stack (M0–M5)

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

## Authorized document answers (M3)

`up` applies `0005_documents`. Then seed the fixture corpus through the API (development only). It uses the department administrators' development credentials and uploads `backend/fixtures/documents` with the permissions in `corpus.toml`:

```bash
python3 scripts/m3_demo.py seed      # uploads, then waits for ingestion; prints status per document
python3 scripts/m3_demo.py capture   # authorized answer + opened citation, and the no-sources response
```

Ingestion needs embeddings. With no gateway (the default), documents stay `queued` with `embedding_unavailable` and are retried every 30 s. After three attempts they show `failed`, and an administrator can reindex them (`POST /api/v1/documents/{id}/reindex`, or re-run `seed` after reindexing). For a labelled local demo:

```bash
MODEL_MODE=fake docker compose up -d --wait api worker scheduler
```

In the web UI, administrators (for example `access.admin@niletech.example`) see an upload form; everyone sees the documents they may read. Choose **Documents you can access** (or Automatic) under **Ask**, and click a citation to open the exact passage of the cited version.

**Retrieval measurement** (dense vs dense + sparse on the frozen dev cases, real Qdrant):

```bash
cd backend
MODEL_MODE=fake .venv/bin/python -m app.cli retrieval-eval --qdrant http://127.0.0.1:6333   # harness check only
MODEL_MODE=gateway LLM_BASE_URL=... MODEL_GATEWAY_TOKEN=... .venv/bin/python -m app.cli retrieval-eval --qdrant <url>
```

Only the gateway run (BGE-M3) is a retrieval-quality result. See `ml/model-gateway/README.md` for running it in Colab next to Qdrant.

## Drive sync, admin console and tenant configuration (M4)

After pulling M4: `python scripts/dev.py up` (runs migration 0007), then `python scripts/dev.py seed` once. The seed adds the new admin capabilities and the tenant settings from `foundation.toml`. NileTech gets both packs, upload and Google Drive, and the 15-minute demo sync. The isolation tenant gets documents and upload only, with a nightly sync.

- Admin console: sign in as `access.admin@niletech.example` (tenant-wide) or `finance.admin@niletech.example` (finance only). The Administration card shows Sources, Documents, Users, Roles & groups, Audit and Organization. Only tabs the caller's capabilities allow are shown; the backend enforces them regardless.
- Google Drive needs operator credentials ([contracts/admin.md](contracts/admin.md#drive-connector-configuration-operator)). Put the JSON and any key file in `./var/connectors/`, set `DRIVE_CONNECTOR_CONFIG=/run/eaios/connectors/drive.json` in `.env`, and run `python scripts/dev.py up`. Without it, creating a source returns `drive_not_configured`.
- The beat task `dispatch-due-connector-syncs` runs every minute; due sources sync every 15 minutes (demo) or nightly after 02:00 UTC. **Sync now** in the Sources tab runs one at once.
- A Drive share maps to a tenant user only if the emails match exactly. For the live demo, add a reader whose email is a real Google account. The command copies `finance.reader`'s roles, groups and attributes:
  `docker compose run --rm -v "$PWD/var:/app/var" migrate python -m app.cli add-drive-reader --email <google account>`
- Live acceptance: `python3 scripts/m4_drive_acceptance.py --folder <folder id> --file-id <test file id> --reader <google account>`. It waits for the exact run each sync request created, exits nonzero on failure or timeout, and always writes evidence (see `docs/evidence/m4/README.md`).
- Drive **group** shares grant access only with verified Google membership (`group_membership: "directory"`, Workspace only). Otherwise use direct user shares.

## Legal pack, GitHub repositories and the desktop app (M5)

After pulling M5: `python3 scripts/dev.py up` (runs migration 0011), then `python3 scripts/dev.py seed` once. The seed enables the `legal` and `code` packs and the `github` and `local_folder` connectors for NileTech. Add `app://eaios` to `CORS_ALLOWED_ORIGINS` in `.env` if you run the desktop app (the new `.env.example` has it).

- **Legal pack.** Upload the three synthetic contracts in `backend/fixtures/legal/` (permissions in `legal.toml`) as `access.admin`, then ask under **Contracts**, for example "When do our contracts expire?" or "Compare the termination clauses of the Aswan and Red Sea agreements". No model is needed; the answer cites the exact sentences and every value is re-derived from them.
- **GitHub.** Put a fine-grained, repository-scoped token in `./var/connectors/github-token`, write `./var/connectors/github.json` ([contract](contracts/m5.md#operator-configuration)), set `GITHUB_CONNECTOR_CONFIG=/run/eaios/connectors/github.json` and run `up`. In the admin console: **Roles & groups → GitHub logins** links a login to a user; **Sources → GitHub repository** adds the repository; **Sync now**. Ask under **Code repositories**.
- **Live GitHub acceptance** (needs a dedicated test repository with the fixture files, a second GitHub account as read collaborator, and the token above; [setup](evidence/m5/OPEN-ITEMS-SETUP.md#1-dedicated-private-github-test-repository)): `python3 scripts/m5_github_acceptance.py --repo <owner>/eaios-m5-acceptance --login <second-account> --mutate gh`. Read-only response-shape check with your own `gh` login: `backend/.venv/bin/python scripts/m5_github_shape_check.py --repo <owner/name>`.
- **Desktop app** (Node 20.19+; the first `npm install` downloads Electron, about 110 MB):

  ```bash
  npm install
  npm run build -w @eaios/web
  EAIOS_API_BASE_URL=http://localhost:8000 npm start -w @eaios/desktop
  ```

  Sign in, then **Local folders → Add a folder**. Only you can read the files until you share the folder with one of your groups. Folder state and the offline queue live in the app-data folder (`~/.config/@eaios/desktop/local-folders` on Linux, `%APPDATA%\@eaios\desktop\local-folders` on Windows); the session is never stored.
- **Desktop tests.** Engine tests: `npm test -w @eaios/desktop`. Smoke against the mock backend (any OS): `npm run smoke:mock -w @eaios/desktop`. Live smoke against the real backend: against the running stack (any OS, standard-library Python; start the stack with `MODEL_MODE=fake` and seed it) `python3 scripts/m5_desktop_smoke.py --api http://localhost:8000 --out var/m5-desktop-smoke.json`, or on a disposable database with the test services from "Checks" `cd backend && .venv/bin/python ../scripts/m5_desktop_smoke.py --disposable --out ../var/m5-desktop-smoke.json`. Windows: [setup](evidence/m5/OPEN-ITEMS-SETUP.md#2-windows-smoke-against-the-real-backend).

## Approved email and scheduled briefings (M6)

After pulling M6: `python3 scripts/dev.py up` (starts Mailpit and runs migration 0012), then `python3 scripts/dev.py seed` once, which enables the `actions` pack and the `mailpit` connector for NileTech (or enable them as `access.admin` under **Organization**). Mailpit's inbox is at <http://localhost:8025>; nothing leaves the machine.

- **Use.** Ask a sales question (for example "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?"). When the verified report appears, **Email and briefings → Create draft for review**, choose a recipient from your organization, edit if needed, **Approve this version**, then **Send now**. Any edit cancels the approval. Recipients must themselves be allowed to see the report.
- **Briefings.** **Schedule briefing** with a sales question and a recipient. Each run uses your permissions at that moment and leaves a draft to review; nothing is sent automatically.
- **Live demo** (stack running with `MODEL_MODE=fake` or a model gateway, seeded): `python3 scripts/m6_demo.py`. It enables the pack and connector as the access admin, publishes the Q1 report, shows the unauthorized-recipient, stale, forged and reused approval rejections, sends the approved version and finds exactly one message in Mailpit by its Message-ID. Evidence goes to `var/m6-demo-<timestamp>.json`.
- **Delivery states.** `uncertain` means Mailpit may have accepted the message but the app could not record it; the scheduler reconciles it by Message-ID every 30 s and never re-sends it blindly. Mailpit keeps messages on the `mailpit-data` volume. An uncertain send is re-sent automatically only when the app recorded that its attempt never reached SMTP. If it may have reached SMTP and its message is not in Mailpit (for example it was deleted from the inbox), it stays `uncertain` (`receipt_missing_after_smtp`) for an operator to resolve. Upgrading to migration 0015 marks sends that were in progress, uncertain or re-queued under older M6 code the same way: delivered if Mailpit has them, otherwise uncertain.
- **Tests from the host** need Mailpit too: `TEST_MAILPIT_SMTP_HOST=<mailpit container IP> TEST_MAILPIT_API_URL=http://<mailpit container IP>:8025` in addition to the variables under "Checks"; `python3 scripts/dev.py test-backend` sets them inside Compose.

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
