# M5 acceptance evidence

Branch `feature/m5-wave-2`, prepared 21 November 2026. Each M5 requirement from the roadmap is mapped to the test that exercises it, the result, how live that result is, and the command that reproduces it. **Live** means a real external system or the real desktop app took part; **mocked** means a test double stood in for it. The fake model never produced a legal result (the legal pack uses no model); where it was used for embeddings or code answers it is labelled.

Environment of the local runs (21–24 November 2026): Debian 13 (kernel 6.12), Python 3.13.5, Node 20.19.2, PostgreSQL 17.6, Qdrant 1.15.4 and Redis 7.4.5 test containers; Electron 39.8.10 (Node 22, Chromium) on the X11 display.

## Requirement → result

| # | Requirement (roadmap M5) | Test / run | Result | Kind | Reproduce |
| --- | --- | --- | --- | --- | --- |
| L1 | Clause comparison cites the exact source passage of each contract | `test_m5_legal.py::test_clause_comparison_cites_each_contract` (+ citation reopen with integrity check) | pass | real PostgreSQL/Qdrant, no model | legal tests below |
| L2 | Expiry answers cite the exact passages; computed dates re-derived (effective + term, renewal deadline) | `test_m5_legal.py::test_expiry_answers_cite_the_exact_passages`; `test_m5_legal_extract.py` against the hand-computed `fixtures/legal/legal.toml` (20 cases) | pass | as above | legal tests |
| L3 | Inaccessible documents cannot contribute | `::test_an_unreadable_contract_cannot_contribute_to_a_comparison`, `::test_only_an_unreadable_contract_looks_like_a_missing_one`, `::test_access_revoked_before_publication_publishes_nothing` | pass | as above | legal tests |
| L4 | Legal pack routing and backend enforcement | `::test_year_filter_and_auto_routing`, `::test_legal_pack_is_backend_enforced`, `unit/test_admin_scope.py::test_m5_routing_*` | pass | as above | legal tests |
| G0 | Check access to a dedicated test repository first | `gh repo list` on the signed-in account: **no dedicated test repository exists**; read-only live check of response shapes on the project repository (`2026-11-21-github-shape-check.json`: repository, collaborators, commit, 454-entry tree, blob id) | shapes pass; **live sync open** | **live** (read-only GitHub), no sync | `scripts/m5_github_shape_check.py` |
| G1 | Repository visibility handled | `test_m5_github.py::test_public_visibility_never_widens_access` (visibility audited, never widens) | pass | **mocked GitHub** | GitHub tests |
| G2 | Revocation | `::test_collaborator_removed_in_github_loses_access_at_the_next_sync`, `::test_unlinking_a_login_revokes_at_once_without_a_sync`, `::test_lost_repository_access_fails_closed[inaccessible/collaborators/renamed]`, `::test_allowlist_removal_fails_before_any_github_request`, `::test_disabled_github_connector_denies_repository_content` | pass | **mocked GitHub** | GitHub tests |
| G3 | Deletion | `::test_edit_rename_and_delete_follow_the_repository` (file delete/rename tombstoned), `::test_deleting_a_repository_source_tombstones_its_documents` | pass | **mocked GitHub** | GitHub tests |
| G4 | Code answers cite repository, file, revision and lines | `::test_code_answer_cites_repository_file_revision_and_exact_lines` (lines computed independently, leading blank lines included), `unit/test_m5_code_and_paths.py` | pass | **mocked GitHub, fake model** | GitHub tests |
| G5 | Excluded/symlink/submodule/oversized paths never ingested | `::test_sync_records_every_path_and_ingests_only_eligible_files` | pass | **mocked GitHub** | GitHub tests |
| G6 | GitHub HTTP client (pagination, permission mapping, 401/403/404/301/rate limit, truncated tree) | `unit/test_m5_github_client.py` (10) | pass | recorded-shape mock transport | unit tests |
| D1 | Reuse the working React interface | Desktop smoke `renderer_loads_shared_react_app`, `renderer_has_bridge_without_node`, `content_security_policy` | pass | **live** Electron | Debian smoke |
| D2 | Backend assigns ownership and sharing | `test_m5_local_folder.py::test_backend_assigns_ownership_and_only_the_owner_reads`, `::test_sharing_is_backend_assigned_and_revocable`, `::test_registration_is_idempotent_and_owned`, `::test_admin_sees_local_folders_but_cannot_change_their_attributes`; smoke `unshared_user_gets_no_sources` | pass | real backend; **live** in the Debian smoke | local-folder tests, Debian smoke |
| D3 | Recursion, symlinks, excluded files, size limits | `engine.test.ts` "recurses, skips…", `test_m5_local_folder.py::test_rules_are_reapplied_on_the_server`; smoke `initial_sync_recursion_and_exclusions` | pass | engine on real FS; **live** smoke | desktop tests, smoke |
| D4 | Deletes | `::test_delete_event_tombstones_and_answers_stop`, `::test_owner_deleting_the_folder_tombstones_everything`, `::test_a_late_retry_cannot_resurrect_a_deleted_file`; engine "deleted while the app was closed"; smoke `watcher_edit_add_delete_and_dedup` | pass | as above | as above |
| D5 | Offline retries | engine "keeps events queued while offline…", "re-sends a batch whose reply was lost", "pauses without a session"; smoke `offline_retry_same_event_ids` | pass | real FS + HTTP double; smoke outage is **simulated** in the transport | desktop tests, smoke |
| D6 | Duplicate watcher events | engine "ignores duplicate and no-op changes…", "drops a queued upsert whose file changed again"; `::test_duplicate_events_and_unchanged_content_change_nothing`; smoke (two identical writes → one event) | pass | as above | as above |
| D7 | Smoke test on Debian | `2026-11-24-desktop-smoke-debian-live.json`: 10 Electron steps + 2 backend checks (owner's answer cites the folder file; unshared user gets `no_sources`) | **pass** | **live**: real Electron, real FastAPI, PostgreSQL, Qdrant; fake model for embeddings | `scripts/m5_desktop_smoke.py` |
| D8 | Smoke test on Windows | CI job `desktop` on `windows-2022`: engine tests and the real Electron smoke against the **mock backend**; result in the PR checks and artifact `desktop-smoke-Windows` | **pass**: CI run 45408304102, all 10 Electron steps on Windows Server 2022 (`2026-11-24-desktop-smoke-windows-ci-mocked-backend.json`) | **mocked backend** | CI, or `npm run smoke:mock -w @eaios/desktop` on Windows |
| X1 | No regression in M0–M4 | full backend suite | **833 passed** (2026-11-24; Ruff and Mypy clean) | real services | `pytest -q` |

## Reproduction commands

Backend tests need disposable services (runbook, "Checks"):

```bash
export TEST_DATABASE_ADMIN_URL=postgresql+psycopg://postgres:<password>@127.0.0.1:55432/postgres
export TEST_REDIS_URL=redis://127.0.0.1:56379/0
export TEST_QDRANT_URL=http://127.0.0.1:56333
cd backend
.venv/bin/pytest -q tests/integration/test_m5_legal.py tests/integration/test_m5_github.py tests/integration/test_m5_local_folder.py
.venv/bin/pytest -q tests/unit/test_m5_legal_extract.py tests/unit/test_m5_code_and_paths.py tests/unit/test_m5_github_client.py tests/unit/test_admin_scope.py
.venv/bin/pytest -q            # full suite
```

Desktop (from the repository root):

```bash
npm install && npm run build -w @eaios/web
npm test -w @eaios/desktop                 # engine tests
npm run smoke:mock -w @eaios/desktop       # real Electron, mock backend
cd backend && .venv/bin/python ../scripts/m5_desktop_smoke.py --out ../var/m5-desktop-smoke.json   # real Electron, real backend
```

GitHub:

```bash
backend/.venv/bin/python scripts/m5_github_shape_check.py --repo <owner/name>    # read-only, your gh login
python3 scripts/m5_github_acceptance.py --repo <owner/name> --login <collaborator> --path <file> --question "<question>"
```

## Open acceptance items

1. **Live GitHub sync, revocation and deletion (G0–G3 live).** Needed: a dedicated **private** test repository; a fine-grained token restricted to it with Contents: read, Metadata: read and Administration: read (to list collaborators), saved as `var/connectors/github-token`; a second GitHub account added as a read collaborator; then `scripts/m5_github_acceptance.py`. Until then the GitHub results are mocked-GitHub evidence plus a read-only live shape check. The operator's personal `gh` token was deliberately not used as a connector credential.
2. **Windows smoke against the real backend.** CI covers Windows with the real Electron app and a mock backend. A teammate's Windows machine with Docker Desktop can run `scripts/m5_desktop_smoke.py` against the test services (runbook, "Checks") to close this.
3. **Real-model code answers.** Code answers were verified with the labelled fake model. The development NLI checker is a natural-language baseline; its behaviour on code needs a Colab run (M7 measures it).
4. **Compose stack not upgraded locally.** The running development stack still runs M4; `python3 scripts/dev.py up && python3 scripts/dev.py seed` applies migration 0011 and the new settings. CI builds the images.
5. **Installer packaging and code signing** for the desktop app are outside M5.

## Files

| File | What |
| --- | --- |
| `2026-11-24-desktop-smoke-debian-live.json` | Real Electron + real backend on Debian (passed) |
| `2026-11-24-desktop-smoke-debian-mocked-backend.json` | Real Electron + mock backend on Debian, same run as the CI Windows job (passed) |
| `2026-11-24-desktop-smoke-windows-ci-mocked-backend.json` | Real Electron + mock backend on the GitHub Actions `windows-2022` runner (passed) |
| `2026-11-21-github-shape-check.json` | Read-only live GitHub responses parsed by the connector client (counts only) |
