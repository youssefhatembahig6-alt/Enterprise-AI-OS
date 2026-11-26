# M7 functional acceptance — observable behaviour of Enterprise AI OS

This pass replaces the planned M7 dataset expansion. It checks what a user can see and do in the product, not model quality. The checker baseline comparison was done separately (`m7-baseline-run.md` in `docs/evaluation`).

## Closeout status (26 November 2026)

All product bugs this pass found (B1, B2, B3, A11y-1) and the test-harness finding are fixed (PR #18 and the closeout PR). The closeout rerun is recorded in [2026-11-26-closeout.md](2026-11-26-closeout.md).

| Status | Items |
| --- | --- |
| **Passed, rerun at closeout** (disposable stack, standard images, no overrides) | 1a login and API connectivity at web :13000 / API :18000 (UI, plus `smoke_stack.py`); 3a upload reaches **Searchable** without a reload (B2); 5a contract lookup with citations and the correct verification text (B3); B1 non-default API origin; A11y-1 group Save name and save behaviour; the full backend suite in the test container via the `test-backend` service |
| **Passed earlier in this pass, not rerun at closeout** | 1b, 1c, 2, 3b, 3c, 4a–4d, 5e (Debian); 5b GitHub stand-in tests; 6 real-model Colab smoke test (PASS on `4050f19`; reused, no new GPU run) |
| **Passed in earlier milestones, not rerun** | 5c Drive (21 Nov, live); 5d live GitHub (24 Nov, 22/22); 5e Windows desktop smoke (24 Nov) |
| **Blocked by missing setup** (exact needs in [Blockers](#blockers-exact)) | 5b code lookup in the UI and 5d live GitHub (no repository-scoped token); 5c live Drive (no second Google account to act as reader, and a person to remove the share during the run) |
| **Deferred (out of scope for M7 functional acceptance)** | Dataset expansion, model-quality work, fine-tuning, threshold selection, blind release evaluation |


| Item | Value |
| --- | --- |
| Tested commit | `695ce8acb23209cc472e3f03c4ab52a935f0d131`, merged `main` (PR #16), for the Compose walkthrough. The real-model Colab smoke test ran on this branch at `4050f19363011d2c598e525052861f45bba0102a` (clean worktree), which differs from `695ce8a` only by acceptance tooling and evidence. `git diff 695ce8a 4050f19 -- backend/app apps packages` is empty |
| Date | 26 November 2026. Compose walkthrough 02:22–02:50 UTC; GitHub stand-in rerun about 02:55 UTC; Colab real-model smoke test 03:14:07–03:16:38 UTC |
| Host | Debian 13 (kernel 6.12), Docker Compose v5.7.0, Python 3.13.5, Node 20.19.2, Electron 39.8.10 |
| Stack | Disposable Compose project `eaios-m7acc` with its own volumes, image tags and secrets. Web http://localhost:13000, API http://localhost:18000, Mailpit http://localhost:18025. The owner's `eaios` stack (3000/8000/8025) kept running and was not touched |
| Model | **Labelled fake model** (`MODEL_MODE=fake`) for the Compose walkthrough; the UI shows "FAKE INFERENCE" on every run, so that part is evidence of behaviour, not model quality. **Real model** (Qwen3-8B-AWQ through the Colab gateway) for check 6 only |
| UI driver | Claude desktop built-in browser (Chromium) at http://localhost:13000 |
| Fixtures | `seed-dev` users, NileTech `niletech-0.1.6` business data, the M3 corpus (`backend/fixtures/documents`), the M5 legal contracts, and one new upload fixture, `backend/fixtures/m7-acceptance/sales-territory-memo.md` (synthetic, SHA-256 in `SHA256SUMS`) |

**Labels used below.** *Rerun* means it was executed in this pass on the tested commit. *Prior* means evidence from an earlier milestone that was not rerun here. *UI* means it was done through the web UI in the browser. *API* means it was done with the same endpoint the UI calls, because the UI has no control for that step.

## Checklist

| # | Requirement | How | Result | Evidence |
| --- | --- | --- | --- | --- |
| 1a | Login works | UI: sign in as `sales.reader`, `finance.reader` and `access.admin` (organization `niletech`). Also `smoke_stack.py`: wrong password gives 401, revoked session 401, inactive user 401, CORS for :13000 | **PASS** (rerun) | [walkthrough](2026-11-26-ui-walkthrough-compose-fake-model.json), [runners log](2026-11-26-runners-compose-fake-model.log) |
| 1b | Unauthorized user cannot see a restricted document | UI: `sales.reader`'s document list leaves out *Q1 2026 finance commentary* and *Board minutes*. The overdue-invoices question gets "I couldn't find information…". `m3_demo.py capture` shows `no_sources` for `sales.reader` | **PASS** (rerun) | walkthrough; [M3 capture](2026-11-26-m3-capture-compose-fake-model.json) |
| 1c | …or reopen its citation | API: `finance.reader`'s citation `15d21c78…/c1` returns 200 for `finance.reader` (integrity `verified`) and **404** for `sales.reader` and for `denied.reader` (explicit deny) | **PASS** (rerun) | [check1](2026-11-26-check1-citation-reopen.json) |
| 2 | Fixture sales question → verified report, expected total, working citation | UI as `finance.reader`: Sales report (SQL), example question. "Verified": 291 orders, net **USD 80,934,385.00**, tax 11,330,813.90, gross 92,265,198.90, matching frozen case `m1-sales-q1-2026-net-fulfilled`. Citation [2] opens the stored row and the read-only SQL, marked "hash verified". `m2_demo_capture.py` gives the same figures | **PASS** (rerun) | walkthrough; [M2 capture](2026-11-26-m2-capture-compose-fake-model.json) |
| 3a | Upload finishes indexing | UI as `access.admin`: upload the memo (sales, Internal, `all-staff`). API shows `ready`, `searchable: true` within a second. The list label stayed "Not searchable" until re-login (bug **B2**). **Closeout rerun:** the label reaches "Searchable" without a reload | **PASS**; B2 fixed (PR #18), rerun PASS | walkthrough (`b2_api_probe`) |
| 3b | Authorized user asks and opens the cited passage | UI as `sales.reader`: "What is the quarterly visit target for the Delta territory?" gives "Verified against sources … 48 customer visits per field representative [1]". Citation opens memo v1, code points 208–306, "text hash verified" | **PASS** (rerun) | walkthrough |
| 3c | After revocation, access is denied | UI: `access.admin` (second tab) removes `sales.reader` from `all-staff` under **Administration → Users**. In `sales.reader`'s still-open session the same citation returns **404** ("This source is not available to you") and the same question gets "I couldn't find information…" | **PASS** (rerun) | walkthrough |
| 4a | Verified report → email draft | UI: **Create draft for review** to `finance.reader`. Drafts to `sales.reader` and `finance.admin` are refused ("The recipient is not authorized to see this report.") | **PASS** (rerun) | walkthrough |
| 4b | Editing invalidates the old approval | UI: approve v1, then edit the subject. While the edit is unsaved, **Send now** is hidden. **Save as new version** gives v2, "Draft — not approved". API (`m6_demo.py`): sending with the pre-edit approval returns `409 approval_rejected` ("The draft changed after approval"). Forged and reused approvals also return 409 | **PASS** (rerun) | walkthrough; [M6 demo](2026-11-26-m6-demo-compose-fake-model.json) |
| 4c | Fresh approval delivers exactly one message to Mailpit | UI: approve v2, **Send now**, "Delivered to Mailpit", attempts 1, Message-ID `<774e0330-…@eaios.local>`. Mailpit has exactly one message with that Message-ID (subject "… (M7 edit)"), still one after 20 s of reconciliation passes. `m6_demo.py` independently finds exactly one message by its Message-ID | **PASS** (rerun) | walkthrough; M6 demo |
| 4d | Scheduled briefing produces a draft and sends nothing without approval | UI: **Schedule briefing** (daily, to `finance.reader`). Beat ran the occurrence at 02:31:29 UTC and produced a draft with `origin=briefing`, `approval=null`, occurrence `review / awaiting_human_approval`. The Mailpit total stayed 2 from 02:30 to 02:33:38 | **PASS** (rerun) | [check4](2026-11-26-check4-drafts-and-briefing.json) |
| 5a | Legal lookup with working citations | UI as `finance.reader`, Contracts: "When do our contracts expire?" gives Red Sea 30 June 2026, Aswan 31 March 2027, Aswan renewal notice by 31 December 2026. All match `legal.toml`. The restricted NileCloud licence is absent. The Aswan citation opens v1, code points 183–252, "text hash verified". The verification text was wrong (bug **B3**). **Closeout rerun:** it now describes the legal checks | **PASS**; B3 fixed (PR #18), rerun PASS | walkthrough |
| 5b | Code lookup with working citations | **UI: blocked.** The disposable stack has no GitHub source (see the blockers table). **GitHub stand-in, rerun:** `test_code_answer_cites_repository_file_revision_and_exact_lines` and `test_acceptance_runner_passes_every_stage` (the real `scripts/m5_github_acceptance.py`, stages A–E, against the in-memory GitHub double) pass. Pytest exit **0, 25 passed** (incl. all of `test_m5_legal.py`) in 23.15 s | **BLOCKED** (live/UI); stand-in **PASS** (rerun) | [pytest log](2026-11-26-pytest-m5-github-double-and-legal.log) |
| 5c | Google Drive | Not rerun | **BLOCKED**; prior **PASS** 21 Nov (live Drive, fake model) | [M4 evidence](../m4/README.md) |
| 5d | Live GitHub | Not rerun | **BLOCKED**; prior **PASS** 24 Nov (22/22, live GitHub, fake model) and Colab real-model code run 24 Nov (22/22, GitHub double) | [M5 evidence](../m5/README.md) |
| 5e | Electron desktop | `m5_desktop_smoke.py --api http://localhost:18000`: real Electron 39.8.10 app against the real disposable backend, fake model, simulated network outage | **PASS** (rerun, Debian). Windows: prior PASS 24 Nov | [desktop smoke](2026-11-26-desktop-smoke-compose-fake-model.json) |
| 6 | Real-model smoke test (verified sales report → approved draft → Mailpit) in the approved Colab setup | Owner ran [`ml/colab-m7-acceptance/m7_real_model_smoke.ipynb`](../../../ml/colab-m7-acceptance/m7_real_model_smoke.ipynb) on a Colab Tesla T4: `scripts/m6_demo.py` against the real gateway, everything over loopback. Report **published** in 149.6 s with `inference_mode: gateway` on every labelled event (status and done; token/citation events carry no label) and no `FAKE` anywhere; `figures_verified_narrative_dev_checked`, checker scores 0.9929 / 0.9972 / 0.9899 (development baseline, uncalibrated 0.5); net **USD 80,934,385.00**, 291 orders, tax 11,330,813.90, gross 92,265,198.90. Unauthorized recipient 422; pre-edit, forged and reused approvals 409. v2 approved and sent: `delivered`, 1 attempt; Mailpit total **1** message, Message-ID `<7ddd1fb8-6739-4207-b78b-432562e08993@eaios.local>`, body carries the verified figures. Independently re-verified from the raw demo JSON, not only from `checks.json` | **PASS** (real model, clean `4050f19`) | [Colab smoke folder](2026-11-26-colab-real-model-smoke/) |

### Blockers (exact)

| Item | What is missing | What the owner needs to provide |
| --- | --- | --- |
| 5b code lookup in the UI and 5d live GitHub | `var/connectors/github-token` is absent (the 24 November fine-grained token was removed; `github.json` still points at it). Using the owner's `gh` CLI OAuth token instead was deliberately not done: it is broad and not repository-scoped | A fine-grained, read-only token for `youssefhatembahig6-alt/eaios-m5-test` saved as `var/connectors/github-token` ([setup §1.3](../m5/OPEN-ITEMS-SETUP.md#1-dedicated-private-github-test-repository)). Then `python3 scripts/m5_github_acceptance.py --api http://localhost:18000 --credentials var/m7-acceptance/dev-credentials.json --repo youssefhatembahig6-alt/eaios-m5-test --login <second account> --mutate gh` with `GITHUB_CONNECTOR_CONFIG` set in `var/m7-acceptance/.env` |
| 5c Drive | The service-account key and `drive.json` exist. The run needs a second Google account as the reader (`--reader`), and the owner has to remove that account's share in Drive by hand when the runner pauses | The reader's Google address (passed to `add-drive-reader` locally, never committed), a Viewer share of `niletech-travel-policy.md` with it, and someone present to remove the share at the pause. Command: [M4 README](../m4/README.md#drive-access-check-step-1) with `--api http://localhost:18000` |
| ~~6 real-model smoke~~ | Resolved 26 November: the owner ran the notebook; result PASS (row 6) | — |

## Product bugs found (all fixed)

### B1 — The web image's CSP hard-codes the API at `localhost:8000`

**Fixed** in the closeout PR. The image renders `connect-src` from `VITE_API_BASE_URL` at build time, and the disposable stack's nginx override was removed. See [the closeout record](2026-11-26-closeout.md#b1-csp-follows-the-configured-api-origin). The text below describes the original bug.

`apps/web/nginx.conf` sends `connect-src 'self' http://localhost:8000 http://127.0.0.1:8000`, but the frontend build reads its API address from `VITE_API_BASE_URL`. Any other API address builds without error, but the browser then blocks every API call. Teammates on another host or port, or a second stack, get a dead UI.

1. Build and start the frontend with `VITE_API_BASE_URL=http://localhost:18000` (for example `python3 scripts/m7_acceptance.py up` with the workaround mount removed from `infra/acceptance/compose.m7.yaml`).
2. Open http://localhost:13000. The UI shows **"API unreachable — The backend is not responding."**
3. The browser console shows: *Connecting to 'http://localhost:18000/health' violates the following Content Security Policy directive: "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000".*
4. `curl -sI http://localhost:13000/ | grep -i content-security` shows the hard-coded origin. `curl -i -H 'Origin: http://localhost:13000' http://localhost:18000/health` shows the API's CORS is correct.

**Workaround used during the pass (removed at closeout):** `infra/acceptance/nginx.m7.conf` is a copy of `apps/web/nginx.conf` with only the two `connect-src` origins changed to port 18000. `compose.m7.yaml` mounts it over `/etc/nginx/conf.d/default.conf` in the disposable frontend. The built image is otherwise unchanged. Suggested fix: template the CSP from the same build argument (or an env var with `envsubst`) so the CSP and `API_BASE_URL` always agree.

### B2 — A new upload can show "Not searchable" until the page is reloaded (low)

**Fixed** on branch `fix/m7-demo-b2-b3`: cause confirmed, both reads now share one snapshot. See [the fix record](2026-11-26-b2-b3-fix.md).

`GET /api/v1/documents` can briefly report `status: "ready"` with `searchable: false` for a document whose ingestion has just committed. `DocumentsPanel` polls only while the status is `queued`, `parsing` or `indexing`, so it stops on that response and keeps the stale label.

1. As `access.admin`, upload any small Markdown file with valid sharing (fake model, so ingestion is fast).
2. Poll `GET /api/v1/documents` every 50 ms from the moment the POST returns 202. Observed: `t=0.00 (ready, searchable=false)`, then `t=0.05 (ready, searchable=true)`. In the UI the label stayed "Not searchable" until the next sign-in.
3. Also reproduced by the first version of `python3 scripts/m7_acceptance.py seed-legal`, which printed `searchable=False` for 2 of 3 contracts. Three seconds later all were `true`. That version also counted any terminal status as success. Since the review fix, `seed-legal` exits 0 only when every expected contract is present exactly once, `ready` and `searchable`. It keeps waiting through ingestion and this B2 window, and it exits non-zero naming each missing, duplicated, failed or otherwise unready fixture (180 s deadline).

Likely cause (not confirmed with a debugger): `DocumentService._describe` (`backend/app/documents/ingest.py`) takes `active_generation` from the document row read by the list query, and `status` from `store.list_versions`, a separate statement. Under READ COMMITTED, the ingestion commit can land between the two reads. Possible fixes: read both in one statement or snapshot, or have the UI keep polling while `ready && !searchable && permission_state == "verified"`.

### B3 — Contract answers show the SQL-report warning text

**Fixed** on branch `fix/m7-demo-b2-b3`. See [the fix record](2026-11-26-b2-b3-fix.md).

`verificationText()` in `apps/web/src/ReportPanel.tsx` recognises `answer_quotes_verified_dev_checked` and `narrative == "verified"`. Every other label falls through to the SQL-report warning. The legal pack's label `legal_facts_extracted_and_rechecked` (`backend/app/legal/workflow.py`) therefore makes a correctly verified contract answer show, in **warning** style: *"Figures verified exactly against the query result. No written summary is shown because it could not be verified."* There is no query result and no withheld summary.

1. Seed the legal fixtures (`python3 scripts/m7_acceptance.py seed-legal`).
2. Sign in as `finance.reader`, choose **Contracts (clauses and expiry)**, ask "When do our contracts expire?".
3. The answer is correct and cited, but the verification line shows the sentence above.

The code pack likely has the same gap if its label is not handled (not checked in the UI here, because no GitHub source was available).

### A11y-1 — The Users tab's groups Save button has no accessible name

**Fixed** in the closeout PR: the button is named `Save groups of <email>`. The GitHub-login Save buttons under Roles & groups had the same gap; they are now named `Save GitHub login of <email>`.

In **Administration → Users**, each row has a roles **Save** button labelled `Save roles of <email>`. The groups **Save** button next to the groups field has no `aria-label`. A screen-reader user hears a column of identical "Save" buttons and cannot tell which user's groups each one saves.

1. Sign in as `access.admin`, open **Administration → Users**.
2. Inspect the accessibility tree (or run `document.querySelectorAll('button')` in devtools). For `sales.reader@niletech.example` the roles button is named `Save roles of sales.reader@niletech.example`, while the groups button is named only `Save`.

Suggested fix: `aria-label={"Save groups of " + email}`, matching the roles button.

### Test-harness finding (not a product bug) — the backend test container lacks `scripts/`

**Fixed** in the closeout PR. The test image is now built from the repository root with an allowlist context and a checkout layout, `git` is installed, and `.git` is mounted read-only. No extra mounts are needed. See [the closeout record](2026-11-26-closeout.md#backend-test-container).

`tests/integration/test_m5_acceptance_runner.py` loads `Path(__file__).parents[3] / "scripts" / "m5_github_acceptance.py"`. Inside the `backend-tests` image that resolves to `/scripts/…`, but the image copies only `backend/`. Collection therefore fails with `FileNotFoundError: '/scripts/m5_github_acceptance.py'` ([first attempt](2026-11-26-pytest-first-attempt-collection-error.log)). `python3 scripts/dev.py test-backend` uses the same container and would stop at collection too. CI runs pytest from a full checkout, which hides it. This pass mounted `scripts/` read-only at `/scripts` (command below). Suggested fix: add that mount to the `backend-tests` service, or skip the test when the script is absent.

### Observations (not filed as bugs)

- Signing in again after a page reload is by design (the session is not stored).
- An already-open session's identity card and document list keep their sign-in snapshot after an admin change until reload. The server enforces the change regardless (3c).
- The Ask button reads "Run report" for every source, including documents, contracts and code.

## Reproduction

All commands run from the repository root. They touch only the `eaios-m7acc` project. The disposable stack's secrets and fixture passwords live in `var/m7-acceptance/` (git-ignored, 0600) and are never printed.

**Project pinning (review fix, PR #17).** Every Compose call the wrapper makes passes `-p eaios-m7acc`. The CLI flag wins over an inherited `COMPOSE_PROJECT_NAME`, over one inside an env file, and over the `name:` key. Child processes run without `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE`, `COMPOSE_ENV_FILES`, `COMPOSE_PROJECT_DIRECTORY` and `COMPOSE_PATH_SEPARATOR`.

The `compose --` passthrough refuses global `-p`/`--project-name`, `-f`/`--file`, `--env-file` and `--project-directory` (in every spelling) before Compose runs. It checks only options before the subcommand, so `run -p 8080:80` still publishes a port. A `DOCKER_COMPOSE` override carrying those flags is refused too.

Before every Compose call, a read-only `docker compose config` check requires project `eaios-m7acc` and only `eaios-m7acc_*` volumes. To see what it resolves:

```bash
python3 scripts/m7_acceptance.py check-project   # read-only
```

Verified read-only on 26 November with `COMPOSE_PROJECT_NAME=eaios` inherited. Before the fix, `config` resolved to project `eaios` with `eaios_*` volumes (the reviewer's reproduction). After it, `check-project` and `compose -- config` resolve to `eaios-m7acc` with only `eaios-m7acc_*` volumes, also with `COMPOSE_FILE` and `COMPOSE_ENV_FILES` aimed at the normal stack. All nine override spellings exit 1 before Compose runs. No `up` or `down` was run against either stack. `backend/tests/unit/test_m7_acceptance_tooling.py` covers this in CI.

```bash
python3 scripts/m7_acceptance.py init-env      # var/m7-acceptance/.env, MODEL_MODE=fake
python3 scripts/m7_acceptance.py up            # build + start; web :13000, API :18000, Mailpit :18025
python3 scripts/m7_acceptance.py seed          # users, NileTech business data, M3 corpus (waits for ingestion)
python3 scripts/m7_acceptance.py seed-legal    # the three M5 legal contracts; exit 0 only if all 3 are ready + searchable
python3 scripts/m7_acceptance.py runners       # smoke_stack, m2 capture, m3 capture, m6 demo, desktop smoke
```

GitHub stand-in, code-answer and legal integration tests in the disposable test profile (since the closeout PR, no extra mounts are needed):

```bash
python3 scripts/m7_acceptance.py compose -- --profile test run --rm --build backend-tests pytest -q -p no:cacheprovider -rA tests/integration/test_m5_github.py tests/integration/test_m5_acceptance_runner.py tests/integration/test_m5_legal.py
```

The web UI steps are in [DEMO.md](DEMO.md). The runners write their JSON to `var/`; the committed copies here replace the absolute repository path with `<repo>` and were checked to contain none of the stack's passwords, database or Redis secrets, JWT secret, or any bearer token.

### Reset the disposable fixtures

The walkthrough leaves `sales.reader` without groups and adds two documents (`sales-territory-memo`, `m7-race-probe`), a delivered email, a briefing draft and an active daily briefing. To start again from clean fixtures:

```bash
python3 scripts/m7_acceptance.py destroy       # removes ONLY eaios-m7acc containers and volumes
rm var/m7-acceptance/dev-credentials.json      # passwords for the deleted database
python3 scripts/m7_acceptance.py up
python3 scripts/m7_acceptance.py seed
python3 scripts/m7_acceptance.py seed-legal
```

To restore only the revoked user without a full reset: as `access.admin`, open **Administration → Users** and set `sales.reader`'s groups back to `all-staff`. `destroy`, like every wrapper command, is pinned to `-p eaios-m7acc` and checked read-only first, so it never touches the owner's `eaios` project or its volumes. Remove the images with `docker image rm eaios-backend:m7acc eaios-web:m7acc eaios-backend-tests:m7acc` if wanted.

## Colab real-model smoke test

**Result: PASS** on clean commit `4050f19363011d2c598e525052861f45bba0102a`, run by the owner on 26 November 2026, 03:14:07–03:16:38 UTC.

| Item | Value |
| --- | --- |
| Bundle | `m7-smoke-evidence-20261126T031638Z.zip`, SHA-256 `0c0a629afb3357a0ace218fd511befbef3b63959ee420cbd301ed21c8bd4aab4`, 15,741 bytes. **Kept local** (`var/m7-acceptance/`, git-ignored); not committed |
| Committed from it | [`2026-11-26-colab-real-model-smoke/`](2026-11-26-colab-real-model-smoke/): `checks.json`, `versions.json`, `gateway-health.json`, `m6-demo-stdout.txt`, `m6-demo-real-model.json`, byte-identical to the bundle. `SHA256SUMS-bundle.txt` lists every file in the bundle, including the seven service logs that were not committed |
| Runtime | Colab Tesla T4 (15,360 MiB, driver 580.82.07); API 127.0.0.1:18000, gateway 127.0.0.1:8100, Mailpit v1.27.11 on 127.0.0.1; no tunnel; no email left the runtime |
| Models (gateway `/health`, pinned revisions accepted by the backend) | `Qwen/Qwen3-8B-AWQ@4da05a8edb55c6046cce958586c33b61da07bb79`, vLLM 0.11.0, AWQ, fp16, utilization 0.75, context 4096, seed 0; `BAAI/bge-m3@5617a9f6…`; checker `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2…` (**development baseline, not the final trained checker**; threshold 0.5 uncalibrated) |
| Dispatch | `MODEL_MODE=gateway`, `REPORT_DISPATCH=inline`. With inline dispatch the **API** delivers through the durable outbox on its own thread pool (`backend/app/main.py`); the Celery worker and beat were running for recovery and reconciliation. The API log shows the `email.deliver` audit events at 03:16:37 and SMTP acceptance at 03:16:37.50. The bundle's `versions.json` labels delivery "celery worker + beat"; that label was imprecise and is corrected in the runner after this run (metadata only) |

**What was verified, from the raw demo JSON rather than only `checks.json`:**

1. The report stream ends with exactly one `done` (outcome `published`). Every event that carries an inference label (10 `status`, 1 `done`) says `gateway`; the 4 `token` and 4 `citation` events carry none by design. The string `FAKE` appears nowhere in the demo output or the API and worker logs.
2. Verification `figures_verified_narrative_dev_checked`: figures `exact_decimal_match_to_evidence`, query semantics `verified_against_report_definition`, join `verified_no_fan_out`, narrative `verified` with checker scores 0.9929 / 0.9972 / 0.9899.
3. Figures: 291 fulfilled orders; net USD 80,934,385.00; tax USD 11,330,813.90; gross USD 92,265,198.90. This matches frozen case `m1-sales-q1-2026-net-fulfilled` and the M2 real-model run of 21 November.
4. Action safety on the real-model report: a draft to `board.member` was refused `422 action_blocked`; sending with the pre-edit v1 approval, a forged approval and a reused approval each returned `409 approval_rejected`.
5. Delivery: v2 approved and sent, final status `delivered` (`smtp_accepted`, 1 attempt). The fresh Mailpit database holds **exactly one** message in total, to `finance.reader@niletech.example`, subject "Verified report: Fulfilled sales, Q1 2026 (reviewed)", whose body carries the verified figures, the evidence id and the dataset fingerprint.
6. Secret scan: the notebook's own scan passed before the bundle was written. A second scan here (JWT, bearer, GitHub/Hugging Face token patterns, passwords, non-fixture emails, non-loopback addresses) found only package version strings.

**Validator tightened after the run (review fix, PR #17).** The validator that produced the bundle's `checks.json` collected the set of inference labels and dropped events without one. In principle, one gateway-labelled event could have satisfied it. `inference_problems()` now requires:
- an explicit `inference: "gateway"` on the terminal `done` event (plus `verification.inference_mode == "gateway"`) and on every `status` event;
- exactly one `done`, coming last, and at least one `status` event.

`token` and `citation` events may stay unlabelled, but any label must be `gateway`, and any other event type must be labelled. Re-validated on the **unchanged** saved evidence `m6-demo-real-model.json`, the run still passes: 10 status events and 1 done, all labelled `gateway`, no problems. The saved fake-model stream and synthetic mutations (one label removed, only the done labelled, only one status labelled, done mode missing, no done, done not last, unlabelled error, token labelled `fake`) are all rejected (`backend/tests/unit/test_m7_acceptance_tooling.py`). The committed bundle files, including its original `checks.json`, are left as produced.

Reproduce: open https://colab.research.google.com/github/youssefhatembahig6-alt/Enterprise-AI-OS/blob/feature/m7-functional-acceptance/ml/colab-m7-acceptance/m7_real_model_smoke.ipynb, choose **Runtime → Change runtime type → T4 GPU**, run cells 1–5 in order (cell 4 runs `ml/colab-m7-acceptance/run_m7_smoke_colab.py all`; cell 5 downloads `m7-smoke-evidence-<UTC stamp>.zip`). If cell 4 fails, the last `+ …` command or the `SystemExit` message names the failing step.
