# M4 acceptance evidence — Drive sync, scoped administration, tenant configuration

Prepared 21 November 2026 on branch `feature/m4-drive-admin-config`, starting from `origin/main` at `e875759`. Updated the same day after review of `943a3c9`: Drive group access now needs verified Google membership, stale syncs are fenced in the database, the operator allowlist is rechecked on every sync, role-definition edits cannot remove the editor's own admin rights, and the live runner waits for the exact run it requested.

Second review round (after `47200796`):
- an internal group removal or unlink now applies immediately to Drive documents;
- the upgrade to 0009 fails closed for Drive access granted under the earlier rules;
- redelivery of a manual sync run id is safe.

Third review round (after `16811379`):
- two Google groups linked to one internal group now grant independently (migration 0010);
- an abandoned run is closed as superseded when a different run takes over its expired lease.

**Labels.** "Drive double" means the in-memory test double in `tests/integration/drive_support.py`. It is not Google Drive. "Fake inference" means `MODEL_MODE=fake`. No result in this folder comes from live Google Drive.

## Status

| Acceptance item | Status | Evidence |
| --- | --- | --- |
| Drive sync: first import, incremental updates, moves, deletions, sharing changes | **Implemented; tested against the Drive double** | `test_m4_drive_sync.py` (37 tests), `test_drive_client.py` (15 tests, HTTP client against a mocked Drive API) |
| Drive group shares only with verified Google membership | Implemented and tested (double) | `test_group_access_needs_verified_current_google_membership`. The user stays in the internal group throughout. Unknown membership: denied. Verified member: allowed. Google membership revoked: denied. Unknown again after another successful sync: still denied. A direct share on the same file keeps working |
| Internal group removal or unlink applies immediately (no Drive sync) | Implemented and tested (Drive double); mutation-checked (without the live-membership join, 5 of the 7 tests fail) | `test_internal_group_removal_applies_without_a_drive_sync` (membership, link): fresh question `no_sources`, citation reopen 404, replay `access_revoked`, no new sync run. `test_internal_group_removal_blocks_an_active_answer` (membership at draft and at publish, link at publish). `test_direct_share_survives_internal_group_removal` (membership, link) |
| Upgrade fails closed | Implemented and tested | `test_m4_upgrade.py`: a database at 0007 (unverified group grant) and one at 0008 (group-derived user grant) are upgraded to head. The Drive document is denied (`deny.unknown_input`) before any sync, the source is `upgrade_resync_required`, the fingerprints are cleared, and the M3 upload-group document stays readable |
| Two Google groups linked to one internal group | Implemented and tested (Drive double); mutation-checked (without 0010 the sync fails `sync_internal_error`) | `test_two_google_groups_for_one_internal_group_are_independent` (unlink either address first): the sync succeeds with one row per address; unlinking one address keeps access; unlinking the last denies at once, with no sync run in between. `test_upgrade_from_0009_keys_group_grants_by_google_email` covers a database already at 0009 |
| Abandoned run when a different run takes over | Implemented and tested (Drive double); mutation-checked | `test_abandoned_run_is_superseded_when_another_run_takes_over`: manual run A dies, its lease expires, and scheduled run B succeeds. A is `superseded` (`claim_taken_over`) and has `finished_at` without ever being redelivered. A late delivery of A does no Drive work, B's run row and the source's verified time are unchanged, and the document has 1 version. Same-run recovery is still covered by `test_redelivery_recovers_a_dead_worker_without_applying_work_twice` |
| Manual sync redelivery | Implemented and tested (Drive double) | `test_duplicate_delivery_during_and_after_a_run`: no Drive work, one version, run `succeeded` attempt 1. `test_redelivery_recovers_a_dead_worker_without_applying_work_twice`: a duplicate before the lease expires; after expiry, recovery as attempt 2 ends `succeeded`, with each file at exactly 1 version. `test_older_attempt_cannot_overwrite_the_recovered_run`: the old attempt ends `superseded`, and the run row stays `succeeded` attempt 2 |
| Stale syncs cannot restore access | Implemented and tested (double); mutation-checked (with the fence disabled, all three tests fail) | `test_policy_change_during_a_sync_fences_the_running_sync`, `test_newer_worker_after_lease_expiry_fences_the_older_one`, `test_older_worker_cannot_tombstone_or_release_the_newer_claim` |
| Operator allowlist enforced on every sync | Implemented and tested (double) | `test_root_removed_from_allowlist_fails_before_any_drive_request`: no Drive call is made, the run fails `root_not_allowed`, and the previously readable document is denied |
| 15-minute demo / nightly schedule; last verified sync time shown | Implemented and tested | `test_schedule_is_fifteen_minutes_in_demo_and_nightly_otherwise`; Sources tab; Compose worker runs `dispatch_due_syncs` every minute |
| Deny when current permissions cannot be established | Implemented and tested | failed sync, subfolder failure, unreadable item ACL, stale verification, paused source, disabled connector |
| Scoped admin controls (documents, sources, users, roles, audit); department vs tenant scope in the backend | Implemented and tested | `test_m4_admin.py` (31 tests), including `test_role_edit_that_removes_own_identity_admin_is_rejected` (also with only a department-scoped `identity.admin` left) and `test_role_edit_is_allowed_when_another_assignment_preserves_rights` |
| Removing access: next request / before an active answer is published / on reopen | Implemented and tested for deactivation, group removal, role removal and clearance downgrade | `test_removal_applies_to_the_next_request`, `test_removal_during_an_active_run_blocks_publication` (draft, verify, publish), `test_deactivation_during_an_active_run_blocks_publication`, `test_removal_hides_published_answers_on_reopen_and_replay` |
| Two tenants with different packs and connectors, no code change; direct request for a disabled capability | Implemented and tested | `test_m4_tenant_config.py` (8 tests); [Compose smoke](2026-11-21-compose-smoke.json) |
| Live runner reliability (exact run correlation, document-specific citation, nonzero exit) | Implemented and tested without credentials | `test_m4_acceptance_runner.py` (5 tests: delayed worker, earlier success and scheduled run ignored, timeout exits 2 with evidence, wrong document exits 1); `test_sync_request_returns_its_own_run` |
| **Live Drive folder, direct user share: authorized answer → revoke sharing → sync → denied** | **OPEN — not performed (no app credentials, no second reader account)** | Runner ready: `scripts/m4_drive_acceptance.py`. Rehearsal with the Drive double: [revocation run](2026-11-21-drive-double-revocation.json) |
| Admin console browser walkthrough | **OPEN — sign-in blocked** | See "Browser walkthrough" below |
| **Real Workspace group verification** (`group_membership: "directory"` against a real directory) | **OPEN, separate from the direct-share demo** | Needs a Google Workspace or Cloud Identity organization and the Directory read-only member scope. Only the mocked API is tested (`test_drive_client.py`) |

A fake Drive does not complete FR-10 (roadmap). The live item stays open until the runner passes against real Drive.

## Drive access check (step 1)

Checked on 21 November 2026 through the Google Drive connector available to this session. That connector is signed in as a **personal Gmail account**, not as the application.

- A dedicated private test folder now exists: **`eaios-m4-drive-test`**, folder id `1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L`. It contains `niletech-travel-policy.md` (file id `119GR0k59wCVdBRhQqzhWOpGIuAu9qC_e`), which answers the runner's default question. Nothing in it is shared.
- **File permissions: readable.** The permission list of the folder and of the file came back (owner only).
- **Group memberships: not available.** Consumer Google accounts have no API for Google Groups membership. The Admin SDK Directory and Cloud Identity Groups APIs need a Workspace or Cloud Identity organization. Without verified membership, the application leaves every Drive **group** share unresolved: it grants nobody, even when the group is linked to an internal group (ADR 0401). The live demo therefore uses a **user** share. With a Workspace directory, set `"group_membership": "directory"` in the operator file.
- **The application itself has no Drive credentials.** The backend cannot use this session's connector. It needs its own credentials, configured by the operator (below).

### What the owner needs to provide

1. **Application credentials for Drive** (either one):
   - a Google Cloud **service account** key JSON with the Drive API enabled in its project. Add the service account's email to `eaios-m4-drive-test` as an **Editor**: Drive returns the full permission list only to principals who can share. Or
   - an **OAuth client** (client id and secret) plus a **refresh token** for the folder owner, with scope `drive.readonly`.
2. **A second Google account** to act as the demo reader. You will share the test file with it and later remove it. Sharing with a new address sends that account a Google notification email. The owner's own account cannot be used because an owner permission cannot be revoked.
3. Then run, from the repository root:

```bash
cat > var/connectors/drive.json <<'JSON'
{"tenants": {"niletech": {"auth": {"type": "service_account", "key_file": "/run/eaios/connectors/drive-sa.json"}, "allowed_roots": ["1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L"]}}}
JSON
```

```bash
echo 'DRIVE_CONNECTOR_CONFIG=/run/eaios/connectors/drive.json' >> .env && python3 scripts/dev.py up
```

```bash
docker compose run --rm -v "$PWD/var:/app/var" migrate python -m app.cli add-drive-reader --email <reader Google account>
```

Share `niletech-travel-policy.md` with the reader account (Viewer), then:

```bash
python3 scripts/m4_drive_acceptance.py --folder 1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L --file-id 119GR0k59wCVdBRhQqzhWOpGIuAu9qC_e --reader <reader Google account>
```

The runner works like this:
1. Each sync request returns its own `run_id`, and the runner polls exactly that run. An earlier success or a scheduled run never satisfies the wait.
2. Sync 1, then wait for the test file's document to finish ingestion.
3. Ask as the reader. The answer must cite **that** document, and the citation must open.
4. Pause. Remove the reader from the file's sharing in Drive and press Enter.
5. Sync 2 must succeed with `permissions_changed >= 1`.
6. A new run of the same question and a reworded question must both get `no_sources`.
7. The original citation must return 404, and replaying the original run must end with `access_revoked`.

It always writes `var/m4-drive-acceptance-<UTC>.json`, with a timestamp per step, both run ids and a PASS/FAIL per check. Exit codes: 0 PASS, 1 a check failed, 2 timeout, 3 setup error. Copy the file here. Without a Colab gateway, start the stack with `MODEL_MODE=fake` and say so in the evidence (inference is labelled on every event).

## Recorded runs

### Drive-double revocation rehearsal

[`2026-11-21-drive-double-revocation.json`](2026-11-21-drive-double-revocation.json), produced by:

```bash
cd backend && EAIOS_M4_EVIDENCE=../docs/evidence/m4/2026-11-21-drive-double-revocation.json .venv/bin/pytest -q tests/integration/test_m4_drive_sync.py -k revoked
```

Real PostgreSQL, real Qdrant, the M3 ingestion and answer path, **fake** inference and the **Drive double** (refreshed after the review fixes):

| Step | Time (UTC) | Result |
| --- | --- | --- |
| Sync 1 (first import), run `fee8b700…` | verified at 14:41:36.149 | `created: 1`, access `current` |
| Ask as `finance-reader` | — | `published`, one citation; citation opens (200) |
| Unshare in the Drive double, sync 2, run `a4562540…` | verified at 14:41:37.501 | `permissions_changed: 1` |
| Ask again | — | `abstained`, `no_sources` |
| Reopen citation / replay run | — | `404` / `access_revoked`, no content |

### Compose smoke

[`2026-11-21-compose-smoke.json`](2026-11-21-compose-smoke.json): the laptop Compose stack at migration `0008_sync_fencing` after the re-seed, with Celery dispatch and `MODEL_MODE=fake`. The tenant-wide admin's attempt to remove `identity.admin` from their own role gets `422 cannot_remove_own_admin`. Both tenants report their fixture settings. A tenant-wide admin lists 7 users and a finance admin lists finance users only. A reader gets `403` on admin routes. The isolation tenant's direct `mode=report` request gets `403 capability_disabled`. Creating a source without credentials gets `422 drive_not_configured`. A NileTech document answer is published (fake inference).

## Browser walkthrough

Not run. The admin console loads at `http://localhost:3000` in the built-in browser pane, but signing in needs a development password. The seed writes those passwords only to the owner-only file `var/dev-credentials.json`. In this session the Claude Code auto-mode classifier denied reading that file ("Unauthorized Access"). Reading it with another tool would work around that decision, so I did not. To unblock it, either sign in yourself in the browser pane (for example as `access.admin@niletech.example` or `finance.admin@niletech.example`), or add a permission rule that allows reading `var/dev-credentials.json`.

## Test results

Backend (real PostgreSQL 17.6, Redis 7.4.5 and Qdrant 1.15.4 test containers):

```bash
cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy app tests && .venv/bin/pytest -q
```

| Run | Result |
| --- | --- |
| Full suite (local) | 744 passed, 0 skipped, 0 failed |
| M4 subset (`test_m4_drive_sync.py`, `test_m4_admin.py`, `test_m4_tenant_config.py`, `test_m4_upgrade.py`, `test_drive_client.py`, `test_admin_scope.py`, `test_m4_acceptance_runner.py`), 3 repeats (local) | 102 passed each time, 0 skipped |

The CI results for the final commit are in the PR.

## Not done in M4

- Live Drive run (above).
- Browser walk-through of the admin console. It is built and passes lint, typecheck and build, and its API calls are covered by the tests above, but in this session it was not exercised by signing in through a browser.
- Verified Google Groups membership on a real Workspace directory. The code path exists (`group_membership: "directory"`) and is unit-tested against a mocked API only. Without it, Drive group shares deny.
- The Changes API for cheaper incremental listing. Each sync re-reads the tree and every ACL, which is what makes a run "verified" for the small corpus.
