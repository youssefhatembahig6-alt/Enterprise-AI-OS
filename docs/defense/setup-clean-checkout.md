# Clean-checkout setup guide (M8)

This guide takes a fresh clone to a seeded, demo-ready stack. Everything runs locally with the **labelled fake model** (`MODEL_MODE=fake`), disposable fixtures, and Mailpit for email. No GPU, model endpoint, paid API key, Google Drive, or GitHub token is needed.

## Verification record

| Item | Value |
| --- | --- |
| Tested commit | `491dfbaf5ab4237de56f307bd2617c7679789b60` (`main` after PR #19) |
| Checkout | Fresh `git clone https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git`, then `git checkout 491dfba`; no untracked files and no `.env` before starting |
| Date | 27 November 2026, 18:28–18:37 UTC |
| Host | Debian 13 (kernel 6.12.118), 16 GB RAM, no GPU; Docker Engine 29.10.1, Docker Compose v5.7.0, Python 3.13.5, git. Node 20.19.2 was installed but not used |
| Stack | A third disposable Compose project, `eaios-m8def` (web :23000, API :28000, Mailpit :28025, image tags `:m8def`, its own volumes). It ran **next to** the owner's `eaios` stack (3000/8000/8025) and the `eaios-m7acc` stack (13000/18000/18025). Neither was stopped, rebuilt, or reseeded |
| How the project was isolated | `DOCKER_COMPOSE="docker compose -p eaios-m8def -f compose.yaml -f <override>"`. The override, kept outside the repository, changed only the project name, the published ports, and the image tags (same pattern as `infra/acceptance/compose.m7.yaml`). `docker compose config` was checked first: project `eaios-m8def`, only `eaios-m8def_*` volumes |
| Result | **PASS after one prerequisite fix** (see [Missing prerequisites found](#missing-prerequisites-found)). `smoke_stack.py` passed all 12 checks. `m2_demo_capture.py` gave 291 orders and net USD 80,934,385.00, verified. `m3_demo.py capture` and `m6_demo.py` passed, with exactly one Mailpit message by Message-ID. The browser rehearsal passed ([rehearsal.md](rehearsal.md)) |

### Measured times

| Step | Time |
| --- | --- |
| `up` (build and start, Docker layer cache warm) | 63 s |
| No-cache rebuild of the backend and web images | see [rehearsal.md § Timings](rehearsal.md#timings) |
| `seed` + `import-business-data` + document corpus + legal contracts | about 15 s |
| Full reset: remove volumes, `up`, and all seeds | 76 s (cache warm) |

## Prerequisites

| Need | Why | Tested with |
| --- | --- | --- |
| Docker Engine with Compose v2 | Runs the whole stack | 29.10.1 / v5.7.0 |
| Python **3.11 or newer** on the host (standard library only) | Helper scripts. `m3_demo.py` and `m7_acceptance.py` import `tomllib`, which first appeared in 3.11 | 3.13.5 |
| git | Clone. The test container also reads git | — |
| About 3 GB of free RAM | Compose memory limits total about 2.7 GB | 16 GB laptop with two other stacks running |
| About 2 GB of disk for images | Backend 582 MB, PostgreSQL 625 MB, Qdrant 259 MB, SeaweedFS 205 MB, web, Redis and Mailpit under 200 MB together | — |
| Network on the first build | Base images, PyPI and npm packages are fetched during `docker build` | — |
| Free loopback ports | 13000, 18000, 18025 for the presentation stack; 3000, 8000, 8025 for the default stack | — |
| Debian: membership of the `docker` group | Otherwise wrap each command as `sg docker -c "<command>"` | — |

Node is needed only for frontend development and the Electron desktop app. It is not needed for the demo.

## Path A — presentation stack (recommended for the defense)

This is a disposable stack on fixed ports 13000/18000/18025, under Compose project `eaios-m7acc`. It never touches the default `eaios` project. The demo script uses these ports.

```bash
git clone https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git
cd Enterprise-AI-OS
python3 scripts/m7_acceptance.py check-project   # read-only: must say project eaios-m7acc
python3 scripts/m7_acceptance.py init-env        # var/m7-acceptance/.env, MODEL_MODE=fake, mode 0600
python3 scripts/m7_acceptance.py up              # build + start, waits for healthchecks
python3 scripts/m7_acceptance.py seed            # users, NileTech sales data, document corpus
python3 scripts/m7_acceptance.py seed-legal      # 3 contracts; exits 0 only if all are ready and searchable
```

Open http://localhost:13000 (app) and http://localhost:18025 (Mailpit). The header must read **API ready** and **Model inference: FAKE (development double, not a model)**.

To print one user's password (the file is git-ignored, mode 0600; never paste it anywhere):

```bash
python3 -c "import json; print(json.load(open('var/m7-acceptance/dev-credentials.json'))['niletech']['finance.reader@niletech.example'])"
```

> **One `eaios-m7acc` per machine.** The project name is fixed. On a machine that already has an `eaios-m7acc` stack, a second clone running these commands builds and starts **that same project and its volumes**. Check with `docker compose ls` first.

Pre-flight check that sends no email:

```bash
python3 scripts/smoke_stack.py --api http://localhost:18000 --web http://localhost:13000 --origin http://localhost:13000 --credentials var/m7-acceptance/dev-credentials.json
```

Do **not** run `m7_acceptance.py runners` right before presenting. It runs `m6_demo.py`, which leaves an email in Mailpit, and the desktop smoke, which needs `npm install`.

## Path B — default development stack

Use this path on a machine that does **not** already run an `eaios` stack.

```bash
git clone https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS.git
cd Enterprise-AI-OS
mkdir -p var/connectors                  # REQUIRED before the first `up` (see finding 1)
python3 scripts/dev.py init-env          # .env with random secrets, mode 0600
# Edit .env and set MODEL_MODE=fake for a model-free demo (otherwise runs end "model unavailable")
python3 scripts/dev.py up
python3 scripts/dev.py seed              # credentials -> var/dev-credentials.json (0600)
python3 scripts/dev.py import-business-data
python3 scripts/m3_demo.py seed          # document corpus, waits for ingestion
```

Then open http://localhost:3000, with Mailpit at http://localhost:8025. To use the legal contracts on this path, upload `backend/fixtures/legal/files/*` as `access.admin` with the permissions in `legal.toml`, as the runbook's M5 section describes.

## Missing prerequisites found

| # | Finding | Effect | Workaround (used here) | Product fix |
| --- | --- | --- | --- | --- |
| 1 | On a fresh clone, `var/` does not exist. `dev.py up` bind-mounts `./var/connectors`, so Docker creates `var/` and `var/connectors/` **owned by root** | `dev.py seed` commits the users and then fails with `PermissionError: '/app/var/.dev-credentials.json.…tmp'`. The users exist but have no saved passwords | Before `up`: `mkdir -p var/connectors`. **If it already happened:** `docker run --rm --user 0 --entrypoint chown -v "$PWD/var:/v" eaios-backend:m4 -R "$(id -u):$(id -g)" /v`, then `docker compose run --rm --user "$(id -u):$(id -g)" -v "$PWD/var:/app/var" migrate python -m app.cli seed-dev --reset-passwords --credentials-file /app/var/dev-credentials.json` (the runbook's recovery command) | Out of scope for this PR: have `init-env` create `var/connectors`, or ship a tracked `var/connectors/.gitkeep`. Path A is not affected, because `init-env` creates `var/m7-acceptance/` as the user before `up` |
| 2 | `compose.yaml` fixes `name: eaios` and tags images `eaios-backend:m4` / `eaios-web:m4` | A second clone running Path B on the same machine **reuses the existing `eaios` project and volumes and retags its images** | Use Path A, or set `DOCKER_COMPOSE` to a command with its own `-p` and an override for ports and image tags (as the verification run did) | Document only |
| 3 | Host scripts need Python ≥ 3.11 (`tomllib`) | `m3_demo.py` / `m7_acceptance.py` fail on import with older Python | Install Python 3.11+ | Document only |

The verification run hit finding 1 and recovered with the commands above. Every later step passed unchanged.

## Optional integrations (not needed for the demo)

| Integration | Needs | Status |
| --- | --- | --- |
| Live Google Drive | Service-account JSON in `var/connectors/`, `DRIVE_CONNECTOR_CONFIG`, a second Google account as reader, and someone to remove the share during the run | Not set up on 27 Nov. Last live pass 21 Nov ([M4 evidence](../evidence/m4/README.md)) |
| Live GitHub | A fine-grained read-only token for the test repository in `var/connectors/github-token`, and `GITHUB_CONNECTOR_CONFIG` | Token absent on 27 Nov. Last live pass 24 Nov ([M5 evidence](../evidence/m5/README.md)) |
| Real model | Colab T4 gateway ([ml/model-gateway/README.md](../../ml/model-gateway/README.md)) | Reused evidence only; see [architecture-and-evidence.md](architecture-and-evidence.md) |
| Electron desktop | Node 20.19+, `npm install` (downloads about 110 MB) | Runbook M5 section |

## Tear down

```bash
python3 scripts/m7_acceptance.py destroy        # Path A: removes ONLY eaios-m7acc containers and volumes
rm var/m7-acceptance/dev-credentials.json       # passwords for the deleted database
```

`destroy` does not delete anything else in `var/m7-acceptance/`. On the owner's machine that folder also holds the private Colab bundle, so never `rm -rf` it. For Path B, the runbook's labelled `DESTROY-LOCAL-DATA` command removes the **default** project's data. Never run it on a machine whose `eaios` data you want to keep.
