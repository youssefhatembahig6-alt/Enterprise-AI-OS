# M5 acceptance evidence

Branch `feature/m5-wave-2`, PR #8. Each M5 requirement from the roadmap is mapped to the check that exercises it, its result, whether it was **live** or **mocked**, and the command that reproduces it.

- **Live** means the real external system or the real desktop app took part.
- **Mocked** means a test double stood in for that system. The GitHub double and the fake model are mocked; the application under test (API, PostgreSQL, Qdrant, ingestion, authorization) is always real.
- The legal pack uses no model, so its results are complete in every mode.

**Tested commit:** `6b0f2002697ac1ec233ccbd0f5876d114d6a17c4` (application code; the evidence commit that follows adds only this folder). Local runs on 24 November 2026: Debian 13 (kernel 6.12), Python 3.13.5, Node 20.19.2, PostgreSQL 17.6, Qdrant 1.15.4 and Redis 7.4.5 test services, Electron 39.8.10. CI: workflow `ci`, 8 jobs per run (7 workflow jobs; `desktop` runs on `ubuntu-24.04` and `windows-2022`), result on the PR head in [CI](#ci).

Missing accounts and platforms keep items **open**; their exact setup is in [OPEN-ITEMS-SETUP.md](OPEN-ITEMS-SETUP.md).

## Requirement → result

| # | Requirement (roadmap M5) | Check | Result | Live / mocked | Reproduce (§ below) |
| --- | --- | --- | --- | --- | --- |
| L1 | Clause comparison cites the exact source passage of each contract | `test_m5_legal.py::test_clause_comparison_cites_each_contract` (citation reopened, integrity checked) | pass | real backend; no model involved | R1 |
| L2 | Expiry answers cite exact passages; derived dates re-derived | `test_m5_legal.py::test_expiry_answers_cite_the_exact_passages`; `test_m5_legal_extract.py` (20) against the hand-computed `backend/fixtures/legal/legal.toml` | pass | real backend; no model | R1, R2 |
| L3 | Inaccessible documents cannot contribute | `::test_an_unreadable_contract_cannot_contribute_to_a_comparison`, `::test_only_an_unreadable_contract_looks_like_a_missing_one`, `::test_access_revoked_before_publication_publishes_nothing` | pass | real backend; no model | R1 |
| L4 | Legal pack routing and backend enforcement | `::test_year_filter_and_auto_routing`, `::test_legal_pack_is_backend_enforced`, `test_admin_scope.py::test_m5_routing_*` | pass | real backend | R1, R2 |
| G0 | Check access to a dedicated test repository first | `gh repo list`: **no dedicated test repository exists**. Read-only live response check on the project repository: `2026-11-21-github-shape-check.json` | shapes pass; **test repository: open** | **live** (read-only) | R4; setup §1 |
| G1 | Repository visibility | `test_m5_github.py::test_public_visibility_never_widens_access` (audited; never widens) | pass | **mocked GitHub**; live: open | R1 |
| G2 | Revocation | Integration: `::test_collaborator_removed_in_github_loses_access_at_the_next_sync`, `::test_unlinking_a_login_revokes_at_once_without_a_sync`, `::test_lost_repository_access_fails_closed[3 cases]`, `::test_allowlist_removal_fails_before_any_github_request`, `::test_disabled_github_connector_denies_repository_content`. Runner stage E: `E_sync_detects_removal`, `E_revoked_reader_refused`, `E_old_citation_inaccessible` | pass | **mocked GitHub** (integration and runner dry run); **live: open** | R1, R3; setup §1 |
| G3a | File rename and deletion | Integration: `::test_edit_rename_and_delete_follow_the_repository`. Runner stage C, with the reader **still a collaborator** (`C_reader_still_collaborator`): `C_old_citations_inaccessible`, `C_deleted_file_cannot_contribute`, `C_old_path_cannot_contribute`, `C_renamed_file_cited_at_new_commit` | pass | **mocked GitHub**; **live: open** | R1, R3; setup §1 |
| G3b | Source deletion | Integration: `::test_deleting_a_repository_source_tombstones_its_documents` (the reader stays a collaborator: removed content never cited, old citation 404, re-added source answers again). Runner stage D: `D_reader_still_collaborator`, `D_deleted_source_cannot_contribute`, `D_old_citation_inaccessible`, `D_recreated_source_syncs`, `D_reader_answered_again_from_new_source` | pass | **mocked GitHub**; **live: open** | R1, R3; setup §1 |
| G4 | Code answers cite repository, file, revision and lines, verified independently | Integration: `::test_code_answer_cites_repository_file_revision_and_exact_lines` (lines computed independently, leading blank lines and CRLF covered). Runner: every `*_matches_github` / `*_cited_at_new_commit` step reads the file at the cited commit through a **separate channel** and checks repository, path, commit, blob SHA, `line_start`, `line_end`, that the quote lies at exactly those lines, and the permalink; also re-opens the citation through the API. Verifier unit tests: `test_m5_citation_verifier.py` (4: exact citation; each field wrong; off-by-one; quote not ending on the last line) | pass | **mocked GitHub, fake model**; **live: open** | R1, R2, R3; setup §1 |
| G5 | Excluded, symlinked, submodule and oversized paths never ingested | `::test_sync_records_every_path_and_ingests_only_eligible_files` | pass | **mocked GitHub** | R1 |
| G6 | GitHub HTTP client | `test_m5_github_client.py` (10): pagination, permission mapping, 401/403/404/301/rate limit, truncated tree | pass | recorded-shape mock transport | R2 |
| G7 | Real-model code answers | Colab runner `ml/colab-m5/run_m5_code_colab.py` (stages A–E with `--require-gateway`). Wiring dry run in fake mode: all 22 steps pass; with `--require-gateway` the same fake run **fails** (`*_real_model` steps), so a fake run can never pass as real-model evidence | wiring checked; **real model: open** | fake model; Colab: open | setup §3 |
| D1 | Reuse the working React interface | Smoke steps `renderer_loads_shared_react_app`, `renderer_has_bridge_without_node`, `content_security_policy` | pass | **live** Electron (Debian, Windows CI) | R5 |
| D2 | Backend assigns ownership and sharing | `test_m5_local_folder.py::test_backend_assigns_ownership_and_only_the_owner_reads`, `::test_sharing_is_backend_assigned_and_revocable`, `::test_registration_is_idempotent_and_owned`, `::test_admin_sees_local_folders_but_cannot_change_their_attributes`. Smoke: `unshared_user_never_receives_folder_content` (no folder document cited for sales.reader) | pass | real backend; **live** smoke | R1, R5 |
| D3 | Recursion, symlinks, excluded files, size limits | `engine.test.ts` "recurses, skips…"; `::test_rules_are_reapplied_on_the_server`; smoke `initial_sync_recursion_and_exclusions` | pass | engine on real filesystem; **live** smoke | R1, R5, R6 |
| D4 | Deletes | `::test_delete_event_tombstones_and_answers_stop`, `::test_owner_deleting_the_folder_tombstones_everything`, `::test_a_late_retry_cannot_resurrect_a_deleted_file`; engine "deleted while the app was closed"; smoke `watcher_edit_add_delete_and_dedup` | pass | as above | R1, R5, R6 |
| D5 | Offline retries | engine "keeps events queued while offline…", "re-sends a batch whose reply was lost", "pauses without a session"; smoke `offline_retry_same_event_ids` | pass | real filesystem + HTTP double; the smoke's outage is **simulated** in the transport | R5, R6 |
| D6 | Duplicate watcher events | engine "ignores duplicate and no-op changes…", "drops a queued upsert whose file changed again"; `::test_duplicate_events_and_unchanged_content_change_nothing`; smoke (two identical writes → one event) | pass | as above | R1, R5, R6 |
| D7 | Smoke test on Debian | `2026-11-24-desktop-smoke-debian-live.json` (disposable backend) and `2026-11-24-desktop-smoke-debian-compose-stack.json` (the upgraded development stack, same mode the Windows run uses): 10 Electron steps + backend checks each | **pass** | **live**: real Electron, real API, PostgreSQL, Qdrant (fake model for embeddings) | R5 |
| D8 | Smoke test on Windows | CI `desktop (windows-2022)`: engine tests and the real Electron smoke, 10/10 steps (`2026-11-24-desktop-smoke-windows-ci-mocked-backend.json`) | pass with **mocked backend**; **real backend: open** | mocked backend | R6; setup §2 |
| S1 | Local development stack upgraded and seeded | `dev.py up` + `dev.py seed` with backups first; `alembic_version = 0011_m5_wave2`; `/ready` = ready (fake model); NileTech packs `code, documents, legal, sales_reports`, connectors `github, google_drive, local_folder, upload`; `CORS_ALLOWED_ORIGINS` gained `app://eaios` with the existing origins kept; row counts of documents, versions, users, runs, sources and pre-upgrade audit events identical to the pre-upgrade dump | pass | **live** (local stack) | R7 |
| X1 | No regression in M0–M4 | full backend suite | **837 passed** locally (Ruff, Mypy, ESLint, tsc clean); CI 832 passed, 5 skipped (original reference archive absent on the runner) | real services | R1 |

## CI

Workflow `ci` on `6b0f2002697ac1ec233ccbd0f5876d114d6a17c4`, run [45414579920](https://github.com/youssefhatembahig6-alt/Enterprise-AI-OS/actions/runs/45414579920): **8 of 8 jobs passed** on the second attempt.

| Job | Attempt 1 | Attempt 2 (failed jobs re-run) |
| --- | --- | --- |
| backend | **failed**: `test_t10_restricted_and_removed_documents_are_indistinguishable`, median latency difference 22.9% (bound 10%) | passed (832 passed, 5 skipped) |
| frontend, compose, gpu-feasibility-notebook, parser-benchmark, vllm-tokenizer | passed | — |
| desktop (ubuntu-24.04), desktop (windows-2022) | passed | — |

The attempt-1 failure is the M3 timing comparison, not a functional check (event sequences and payloads matched). The commit does not change the search path it measures. Three local runs gave 2.5%, 1.3% and 1.4%, so the 22.9% is treated as shared-runner timing noise; the test bound was **not** loosened. Earlier runs on this PR: `786973f` 8/8 passed; `c32c2b7` 8/8 passed; `aa953c9` failed both desktop jobs (Node 22 `node --test` directory argument), fixed in `c32c2b7`. An earlier report on this PR said 9 jobs; the workflow has **8** (7 jobs, `desktop` on two systems).

## Reproduction commands

**R1 — backend tests** (disposable services as in the runbook, "Checks"):

```bash
export TEST_DATABASE_ADMIN_URL=postgresql+psycopg://postgres:<password>@127.0.0.1:55432/postgres
export TEST_REDIS_URL=redis://127.0.0.1:56379/0
export TEST_QDRANT_URL=http://127.0.0.1:56333
cd backend
.venv/bin/pytest -q tests/integration/test_m5_legal.py tests/integration/test_m5_github.py \
  tests/integration/test_m5_local_folder.py tests/integration/test_m5_acceptance_runner.py
.venv/bin/pytest -q                      # full suite
```

**R2 — unit tests:** `cd backend && .venv/bin/pytest -q tests/unit/test_m5_legal_extract.py tests/unit/test_m5_code_and_paths.py tests/unit/test_m5_github_client.py tests/unit/test_m5_citation_verifier.py tests/unit/test_admin_scope.py`

**R3 — acceptance runner dry run** (stages A–E over HTTP against the real API with the GitHub double on both channels; writes `2026-11-24-github-acceptance-dryrun-mocked-github.json`):

```bash
cd backend && EAIOS_M5_DRYRUN_EVIDENCE=../docs/evidence/m5/2026-11-24-github-acceptance-dryrun-mocked-github.json \
  .venv/bin/pytest -q tests/integration/test_m5_acceptance_runner.py
```

Live run (setup §1): `python3 scripts/m5_github_acceptance.py --repo <owner>/eaios-m5-acceptance --login <second-account> --mutate gh`

**R4 — read-only live GitHub shapes:** `backend/.venv/bin/python scripts/m5_github_shape_check.py --repo youssefhatembahig6-alt/Enterprise-AI-OS`

**R5 — desktop smoke, real backend** (after `npm ci && npm run build -w @eaios/web && npm run build -w @eaios/desktop`):

```bash
cd backend && .venv/bin/python ../scripts/m5_desktop_smoke.py --disposable --out ../docs/evidence/m5/2026-11-24-desktop-smoke-debian-live.json
python3 scripts/m5_desktop_smoke.py --api http://localhost:8000 --out docs/evidence/m5/2026-11-24-desktop-smoke-debian-compose-stack.json   # stack started with MODEL_MODE=fake
```

**R6 — desktop engine tests and the mock-backend smoke:** `npm test -w @eaios/desktop` and `npm run smoke:mock -w @eaios/desktop` (the CI `desktop` job runs both on Ubuntu, under `xvfb-run`, and on Windows)

**R7 — local stack upgrade:**

```bash
mkdir -p var/backups && cp -p .env var/backups/env-before-m5-upgrade
docker exec eaios-postgres-1 pg_dump -U postgres -Fc eaios > var/backups/eaios-before-0011.dump
# add ,app://eaios to CORS_ALLOWED_ORIGINS in .env (other origins unchanged)
MODEL_MODE=fake python3 scripts/dev.py up && python3 scripts/dev.py seed
docker exec eaios-postgres-1 psql -U postgres -d eaios -Atc "select version_num from alembic_version"   # 0011_m5_wave2
curl -s http://localhost:8000/ready
```

## Open acceptance items

| Item | Rows | Why open | What closes it |
| --- | --- | --- | --- |
| Live GitHub sync, rename/delete, source deletion and revocation, with independent citation checks | G0–G4 live | No dedicated private test repository, second GitHub account or scoped connector token exists. The operator's personal `gh` token was deliberately not used as a connector credential | [Setup §1](OPEN-ITEMS-SETUP.md#1-dedicated-private-github-test-repository), then `scripts/m5_github_acceptance.py --mutate gh` |
| Windows smoke against the real backend | D8 | No Windows machine here; CI's Windows run uses the mock backend | [Setup §2](OPEN-ITEMS-SETUP.md#2-windows-smoke-against-the-real-backend) |
| Real-model code answers | G7 | Needs the Colab T4 runtime | [Setup §3](OPEN-ITEMS-SETUP.md#3-real-model-code-answers-colab) |
| Installer packaging and code signing | — | Outside M5 | — |

## Files

| File | What | Live / mocked |
| --- | --- | --- |
| `2026-11-24-desktop-smoke-debian-live.json` | Real Electron + in-process real backend on a disposable database | live (fake model) |
| `2026-11-24-desktop-smoke-debian-compose-stack.json` | Real Electron + the upgraded development stack (`--api` mode) | live (fake model) |
| `2026-11-24-desktop-smoke-debian-mocked-backend.json` | Real Electron + mock backend on Debian (same as the CI job) | mocked backend |
| `2026-11-24-desktop-smoke-windows-ci-mocked-backend.json` | Real Electron + mock backend on the `windows-2022` runner | mocked backend |
| `2026-11-24-github-acceptance-dryrun-mocked-github.json` | Acceptance runner, stages A–E, 22 steps, real API | mocked GitHub, fake model |
| `2026-11-21-github-shape-check.json` | Read-only live GitHub responses parsed by the connector client (counts only) | live, read-only |
| [OPEN-ITEMS-SETUP.md](OPEN-ITEMS-SETUP.md) | Exact setup for the three open items | — |
