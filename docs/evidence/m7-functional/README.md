# M7 functional acceptance — observable behaviour of Enterprise AI OS

This pass replaces the planned M7 dataset expansion. It checks what a user can see and do in the product, not model quality. Dataset expansion, model comparisons, fine-tuning and threshold selection are deferred.

| Item | Value |
| --- | --- |
| Tested commit | `695ce8acb23209cc472e3f03c4ab52a935f0d131`, merged `main` (PR #16). This branch adds only acceptance tooling, fixtures and evidence. `git diff 695ce8a -- backend/app apps packages` is empty |
| Date | 26 November 2026, 02:22–02:50 UTC. GitHub stand-in rerun at about 02:55 UTC |
| Host | Debian 13 (kernel 6.12), Docker Compose v5.7.0, Python 3.13.5, Node 20.19.2, Electron 39.8.10 |
| Stack | Disposable Compose project `eaios-m7acc` with its own volumes, image tags and secrets. Web http://localhost:13000, API http://localhost:18000, Mailpit http://localhost:18025. The owner's `eaios` stack (3000/8000/8025) kept running and was not touched |
| Model | **Labelled fake model** (`MODEL_MODE=fake`) for the whole walkthrough. The UI shows "FAKE INFERENCE" on every run. This is evidence of behaviour, not of model quality |
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
| 3a | Upload finishes indexing | UI as `access.admin`: upload the memo (sales, Internal, `all-staff`). API shows `ready`, `searchable: true` within a second. The list label stayed "Not searchable" until re-login (bug **B2**) | **PASS**, cosmetic bug B2 | walkthrough (`b2_api_probe`) |
| 3b | Authorized user asks and opens the cited passage | UI as `sales.reader`: "What is the quarterly visit target for the Delta territory?" gives "Verified against sources … 48 customer visits per field representative [1]". Citation opens memo v1, code points 208–306, "text hash verified" | **PASS** (rerun) | walkthrough |
| 3c | After revocation, access is denied | UI: `access.admin` (second tab) removes `sales.reader` from `all-staff` under **Administration → Users**. In `sales.reader`'s still-open session the same citation returns **404** ("This source is not available to you") and the same question gets "I couldn't find information…" | **PASS** (rerun) | walkthrough |
| 4a | Verified report → email draft | UI: **Create draft for review** to `finance.reader`. Drafts to `sales.reader` and `finance.admin` are refused ("The recipient is not authorized to see this report.") | **PASS** (rerun) | walkthrough |
| 4b | Editing invalidates the old approval | UI: approve v1, then edit the subject. While the edit is unsaved, **Send now** is hidden. **Save as new version** gives v2, "Draft — not approved". API (`m6_demo.py`): sending with the pre-edit approval returns `409 approval_rejected` ("The draft changed after approval"). Forged and reused approvals also return 409 | **PASS** (rerun) | walkthrough; [M6 demo](2026-11-26-m6-demo-compose-fake-model.json) |
| 4c | Fresh approval delivers exactly one message to Mailpit | UI: approve v2, **Send now**, "Delivered to Mailpit", attempts 1, Message-ID `<774e0330-…@eaios.local>`. Mailpit has exactly one message with that Message-ID (subject "… (M7 edit)"), still one after 20 s of reconciliation passes. `m6_demo.py` independently finds exactly one message by its Message-ID | **PASS** (rerun) | walkthrough; M6 demo |
| 4d | Scheduled briefing produces a draft and sends nothing without approval | UI: **Schedule briefing** (daily, to `finance.reader`). Beat ran the occurrence at 02:31:29 UTC and produced a draft with `origin=briefing`, `approval=null`, occurrence `review / awaiting_human_approval`. The Mailpit total stayed 2 from 02:30 to 02:33:38 | **PASS** (rerun) | [check4](2026-11-26-check4-drafts-and-briefing.json) |
| 5a | Legal lookup with working citations | UI as `finance.reader`, Contracts: "When do our contracts expire?" gives Red Sea 30 June 2026, Aswan 31 March 2027, Aswan renewal notice by 31 December 2026. All match `legal.toml`. The restricted NileCloud licence is absent. The Aswan citation opens v1, code points 183–252, "text hash verified". The verification text is wrong (bug **B3**) | **PASS**, copy bug B3 | walkthrough |
| 5b | Code lookup with working citations | **UI: blocked.** The disposable stack has no GitHub source (see the blockers table). **GitHub stand-in, rerun:** `test_code_answer_cites_repository_file_revision_and_exact_lines` and `test_acceptance_runner_passes_every_stage` (the real `scripts/m5_github_acceptance.py`, stages A–E, against the in-memory GitHub double) pass. Pytest exit **0, 25 passed** (incl. all of `test_m5_legal.py`) in 23.15 s | **BLOCKED** (live/UI); stand-in **PASS** (rerun) | [pytest log](2026-11-26-pytest-m5-github-double-and-legal.log) |
| 5c | Google Drive | Not rerun | **BLOCKED**; prior **PASS** 21 Nov (live Drive, fake model) | [M4 evidence](../m4/README.md) |
| 5d | Live GitHub | Not rerun | **BLOCKED**; prior **PASS** 24 Nov (22/22, live GitHub, fake model) and Colab real-model code run 24 Nov (22/22, GitHub double) | [M5 evidence](../m5/README.md) |
| 5e | Electron desktop | `m5_desktop_smoke.py --api http://localhost:18000`: real Electron 39.8.10 app against the real disposable backend, fake model, simulated network outage | **PASS** (rerun, Debian). Windows: prior PASS 24 Nov | [desktop smoke](2026-11-26-desktop-smoke-compose-fake-model.json) |
| 6 | Real-model smoke test (verified sales report → approved draft → Mailpit) in the approved Colab setup | Notebook [`ml/colab-m7-acceptance/m7_real_model_smoke.ipynb`](../../../ml/colab-m7-acceptance/m7_real_model_smoke.ipynb) runs `scripts/m6_demo.py` against the real Qwen3-8B-AWQ gateway, a Celery worker and Mailpit, all over loopback in one T4 runtime. It passes only if every report event says `gateway`, the run is published, the draft is delivered and Mailpit holds exactly one message | **BLOCKED — waiting for the owner to run it** (needs a Colab T4 session). Dry check: `checks()` correctly rejects the local fake-model output | — |

### Blockers (exact)

| Item | What is missing | What the owner needs to provide |
| --- | --- | --- |
| 5b code lookup in the UI and 5d live GitHub | `var/connectors/github-token` is absent (the 24 November fine-grained token was removed; `github.json` still points at it). Using the owner's `gh` CLI OAuth token instead was deliberately not done: it is broad and not repository-scoped | A fine-grained, read-only token for `youssefhatembahig6-alt/eaios-m5-test` saved as `var/connectors/github-token` ([setup §1.3](../m5/OPEN-ITEMS-SETUP.md#1-dedicated-private-github-test-repository)). Then `python3 scripts/m5_github_acceptance.py --api http://localhost:18000 --credentials var/m7-acceptance/dev-credentials.json --repo youssefhatembahig6-alt/eaios-m5-test --login <second account> --mutate gh` with `GITHUB_CONNECTOR_CONFIG` set in `var/m7-acceptance/.env` |
| 5c Drive | The service-account key and `drive.json` exist. The run needs a second Google account as the reader (`--reader`), and the owner has to remove that account's share in Drive by hand when the runner pauses | The reader's Google address (passed to `add-drive-reader` locally, never committed), a Viewer share of `niletech-travel-policy.md` with it, and someone present to remove the share at the pause. Command: [M4 README](../m4/README.md#drive-access-check-step-1) with `--api http://localhost:18000` |
| 6 real-model smoke | A Colab T4 runtime (owner's Colab Pro account) | Run the notebook (see "Colab real-model smoke test" below) and return `m7-smoke-evidence-<stamp>.zip` |

## Product bugs found (fix in a separate PR)

### B1 — The web image's CSP hard-codes the API at `localhost:8000`

`apps/web/nginx.conf` sends `connect-src 'self' http://localhost:8000 http://127.0.0.1:8000`, but the frontend build reads its API address from `VITE_API_BASE_URL`. Any other API address builds without error, but the browser then blocks every API call. Teammates on another host or port, or a second stack, get a dead UI.

1. Build and start the frontend with `VITE_API_BASE_URL=http://localhost:18000` (for example `python3 scripts/m7_acceptance.py up` with the workaround mount removed from `infra/acceptance/compose.m7.yaml`).
2. Open http://localhost:13000. The UI shows **"API unreachable — The backend is not responding."**
3. The browser console shows: *Connecting to 'http://localhost:18000/health' violates the following Content Security Policy directive: "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000".*
4. `curl -sI http://localhost:13000/ | grep -i content-security` shows the hard-coded origin. `curl -i -H 'Origin: http://localhost:13000' http://localhost:18000/health` shows the API's CORS is correct.

**Workaround in this pass (acceptance only):** `infra/acceptance/nginx.m7.conf` is a copy of `apps/web/nginx.conf` with only the two `connect-src` origins changed to port 18000. `compose.m7.yaml` mounts it over `/etc/nginx/conf.d/default.conf` in the disposable frontend. The built image is otherwise unchanged. Suggested fix: template the CSP from the same build argument (or an env var with `envsubst`) so the CSP and `API_BASE_URL` always agree.

### B2 — A new upload can show "Not searchable" until the page is reloaded (low)

`GET /api/v1/documents` can briefly report `status: "ready"` with `searchable: false` for a document whose ingestion has just committed. `DocumentsPanel` polls only while the status is `queued`, `parsing` or `indexing`, so it stops on that response and keeps the stale label.

1. As `access.admin`, upload any small Markdown file with valid sharing (fake model, so ingestion is fast).
2. Poll `GET /api/v1/documents` every 50 ms from the moment the POST returns 202. Observed: `t=0.00 (ready, searchable=false)`, then `t=0.05 (ready, searchable=true)`. In the UI the label stayed "Not searchable" until the next sign-in.
3. Also reproduced by `python3 scripts/m7_acceptance.py seed-legal`, which printed `searchable=False` for 2 of 3 contracts. Three seconds later all were `true`.

Likely cause (not confirmed with a debugger): `DocumentService._describe` (`backend/app/documents/ingest.py`) takes `active_generation` from the document row read by the list query, and `status` from `store.list_versions`, a separate statement. Under READ COMMITTED, the ingestion commit can land between the two reads. Possible fixes: read both in one statement or snapshot, or have the UI keep polling while `ready && !searchable && permission_state == "verified"`.

### B3 — Contract answers show the SQL-report warning text

`verificationText()` in `apps/web/src/ReportPanel.tsx` recognises `answer_quotes_verified_dev_checked` and `narrative == "verified"`. Every other label falls through to the SQL-report warning. The legal pack's label `legal_facts_extracted_and_rechecked` (`backend/app/legal/workflow.py`) therefore makes a correctly verified contract answer show, in **warning** style: *"Figures verified exactly against the query result. No written summary is shown because it could not be verified."* There is no query result and no withheld summary.

1. Seed the legal fixtures (`python3 scripts/m7_acceptance.py seed-legal`).
2. Sign in as `finance.reader`, choose **Contracts (clauses and expiry)**, ask "When do our contracts expire?".
3. The answer is correct and cited, but the verification line shows the sentence above.

The code pack likely has the same gap if its label is not handled (not checked in the UI here, because no GitHub source was available).

### A11y-1 — The Users tab's groups Save button has no accessible name

In **Administration → Users**, each row has a roles **Save** button labelled `Save roles of <email>`. The groups **Save** button next to the groups field has no `aria-label`. A screen-reader user hears a column of identical "Save" buttons and cannot tell which user's groups each one saves.

1. Sign in as `access.admin`, open **Administration → Users**.
2. Inspect the accessibility tree (or run `document.querySelectorAll('button')` in devtools). For `sales.reader@niletech.example` the roles button is named `Save roles of sales.reader@niletech.example`, while the groups button is named only `Save`.

Suggested fix: `aria-label={"Save groups of " + email}`, matching the roles button.

### Test-harness finding (not a product bug) — the backend test container lacks `scripts/`

`tests/integration/test_m5_acceptance_runner.py` loads `Path(__file__).parents[3] / "scripts" / "m5_github_acceptance.py"`. Inside the `backend-tests` image that resolves to `/scripts/…`, but the image copies only `backend/`. Collection therefore fails with `FileNotFoundError: '/scripts/m5_github_acceptance.py'` ([first attempt](2026-11-26-pytest-first-attempt-collection-error.log)). `python3 scripts/dev.py test-backend` uses the same container and would stop at collection too. CI runs pytest from a full checkout, which hides it. This pass mounted `scripts/` read-only at `/scripts` (command below). Suggested fix: add that mount to the `backend-tests` service, or skip the test when the script is absent.

### Observations (not filed as bugs)

- Signing in again after a page reload is by design (the session is not stored).
- An already-open session's identity card and document list keep their sign-in snapshot after an admin change until reload. The server enforces the change regardless (3c).
- The Ask button reads "Run report" for every source, including documents, contracts and code.

## Reproduction

All commands run from the repository root. They touch only the `eaios-m7acc` project. The disposable stack's secrets and fixture passwords live in `var/m7-acceptance/` (git-ignored, 0600) and are never printed.

```bash
python3 scripts/m7_acceptance.py init-env      # var/m7-acceptance/.env, MODEL_MODE=fake
python3 scripts/m7_acceptance.py up            # build + start; web :13000, API :18000, Mailpit :18025
python3 scripts/m7_acceptance.py seed          # users, NileTech business data, M3 corpus (waits for ingestion)
python3 scripts/m7_acceptance.py seed-legal    # the three M5 legal contracts
python3 scripts/m7_acceptance.py runners       # smoke_stack, m2 capture, m3 capture, m6 demo, desktop smoke
```

GitHub stand-in, code-answer and legal integration tests in the disposable test profile (scripts mounted read-only, see the test-harness finding above):

```bash
python3 scripts/m7_acceptance.py compose -- --profile test run --rm -v "$PWD/scripts:/scripts:ro" backend-tests pytest -q -p no:cacheprovider -rA tests/integration/test_m5_github.py tests/integration/test_m5_acceptance_runner.py tests/integration/test_m5_legal.py
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

To restore only the revoked user without a full reset: as `access.admin`, open **Administration → Users** and set `sales.reader`'s groups back to `all-staff`. `destroy` uses `-p eaios-m7acc` explicitly and never touches the owner's `eaios` project or its volumes. Remove the images with `docker image rm eaios-backend:m7acc eaios-web:m7acc eaios-backend-tests:m7acc` if wanted.

## Colab real-model smoke test

Open the notebook from GitHub in Colab, choose **Runtime → Change runtime type → T4 GPU**, and run cells 1–5 in order:

https://colab.research.google.com/github/youssefhatembahig6-alt/Enterprise-AI-OS/blob/feature/m7-functional-acceptance/ml/colab-m7-acceptance/m7_real_model_smoke.ipynb

- Cell 2 installs the pinned vLLM environment (several minutes). Cell 3 launches the gateway on loopback. **Cell 4** runs `ml/colab-m7-acceptance/run_m7_smoke_colab.py all`: PostgreSQL 17, Redis, Qdrant, the hash-locked backend, the NileTech import, Mailpit v1.27.11, the API, a Celery worker and beat, then `scripts/m6_demo.py` and the independent checks. It prints `smoke test PASSED` or `FAILED` and `checks.json`.
- Cell 5 downloads **`m7-smoke-evidence-<UTC stamp>.zip`** (secret-scanned before it is written). Return that file. It contains `m6-demo-real-model.json`, `checks.json`, `gateway-health.json`, `versions.json`, `m6-demo-stdout.txt` and the service logs.
- No tunnel is opened and no email leaves the runtime, so the Colab remote-proxy question does not arise.
- If cell 4 fails, the step that failed is the last `+ …` command or the `SystemExit` message in its output. Return that output (it holds no secrets) instead of the zip.
