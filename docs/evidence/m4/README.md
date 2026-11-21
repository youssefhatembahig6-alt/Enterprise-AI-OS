# M4 acceptance evidence — Drive sync, scoped administration, tenant configuration

Prepared 21 November 2026 on branch `feature/m4-drive-admin-config`, starting from `origin/main` at `e875759`.

## Status

| Acceptance item | Status | Evidence |
| --- | --- | --- |
| Drive sync: first import, incremental updates, moves, deletions, sharing changes | **Implemented; tested against a Drive test double** | `test_m4_drive_sync.py` (18 tests), `test_drive_client.py` (10 tests, HTTP client against a mocked Drive API) |
| 15-minute demo / nightly schedule; last verified sync time shown | Implemented and tested | `test_schedule_is_fifteen_minutes_in_demo_and_nightly_otherwise`; Sources tab; Compose worker runs `dispatch_due_syncs` every minute |
| Deny when current permissions cannot be established | Implemented and tested | failed sync, subfolder failure, unreadable item ACL, stale verification, paused source, disabled connector |
| Scoped admin controls (documents, sources, users, roles, audit); department vs tenant scope in the backend | Implemented and tested | `test_m4_admin.py` (29 tests) |
| Removing access: next request / before an active answer is published / on reopen | Implemented and tested for deactivation, group removal, role removal and clearance downgrade | `test_removal_applies_to_the_next_request`, `test_removal_during_an_active_run_blocks_publication` (draft, verify, publish), `test_deactivation_during_an_active_run_blocks_publication`, `test_removal_hides_published_answers_on_reopen_and_replay` |
| Two tenants with different packs and connectors, no code change; direct request for a disabled capability | Implemented and tested | `test_m4_tenant_config.py` (8 tests); [Compose smoke](2026-11-21-compose-smoke.json) |
| **Live Drive folder: authorized answer → revoke sharing → sync → denied** | **OPEN — blocked on account access** | Runner ready: `scripts/m4_drive_acceptance.py`. Double-backed rehearsal: [revocation run](2026-11-21-drive-double-revocation.json) |

A fake Drive does not complete FR-10 (roadmap). The live item stays open until the runner passes against real Drive.

## Drive access check (step 1)

Checked on 21 November 2026 through the Google Drive connector available to this session. That connector is signed in as a **personal Gmail account**, not as the application.

- A dedicated private test folder now exists: **`eaios-m4-drive-test`**, folder id `1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L`. It contains `niletech-travel-policy.md` (file id `119GR0k59wCVdBRhQqzhWOpGIuAu9qC_e`), which answers the runner's default question. Nothing in it is shared.
- **File permissions: readable.** The permission list of the folder and of the file came back (owner only).
- **Group memberships: not available.** Consumer Google accounts have no API for Google Groups membership. The Admin SDK Directory and Cloud Identity Groups APIs need a Workspace or Cloud Identity organization. M4 therefore maps a Drive group permission to an internal group through an explicit link, and group membership is managed in the application (ADR 0401). The live demo uses a **user** share.
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
python3 scripts/m4_drive_acceptance.py --folder 1aXhdmdxjQTiNXBoZE4Edi1yR_ToUb28L --reader <reader Google account>
```

The runner syncs, asks as the reader, opens the citation, and then pauses. Remove the reader from the file's sharing in Drive and press Enter. The runner syncs again, asks again (expects `no_sources`), reopens the citation (expects `404`) and replays the first run (expects `access_revoked`). It writes `var/m4-drive-acceptance-<UTC>.json` with every timestamp, both sync runs and a PASS/FAIL per check. Copy that file here. Without a Colab gateway, start the stack with `MODEL_MODE=fake` and say so in the evidence (inference is labelled on every event).

## Recorded runs

### Drive-double revocation rehearsal

[`2026-11-21-drive-double-revocation.json`](2026-11-21-drive-double-revocation.json), produced by:

```bash
cd backend && EAIOS_M4_EVIDENCE=../docs/evidence/m4/2026-11-21-drive-double-revocation.json .venv/bin/pytest -q tests/integration/test_m4_drive_sync.py -k revoked
```

Real PostgreSQL, real Qdrant, the M3 ingestion and answer path, **fake** inference and the **in-memory Drive double**:

| Step | Time (UTC) | Result |
| --- | --- | --- |
| Sync 1 (first import) | verified at 14:06:51.475 | `created: 1`, access `current` |
| Ask as `finance-reader` | — | `published`, one citation; citation opens (200) |
| Unshare in Drive (double), sync 2 | verified at 14:06:52.855 | `permissions_changed: 1` |
| Ask again | — | `abstained`, `no_sources` |
| Reopen citation / replay run | — | `404` / `access_revoked`, no content |

### Compose smoke

[`2026-11-21-compose-smoke.json`](2026-11-21-compose-smoke.json): the laptop Compose stack after migration 0007 and the re-seed, with Celery dispatch and `MODEL_MODE=fake`. Both tenants report their fixture settings. A tenant-wide admin lists 7 users and a finance admin lists finance users only. A reader gets `403` on admin routes. The isolation tenant's direct `mode=report` request gets `403 capability_disabled`. Creating a source without credentials gets `422 drive_not_configured`. A NileTech document answer is published (fake inference).

## Test results

See "Test results" in the PR description for the exact commands and counts at the final commit.

## Not done in M4

- Live Drive run (above).
- Browser walk-through of the admin console. It is built and passes lint, typecheck and build, and its API calls are covered by the tests above, but in this session it was not exercised by signing in through a browser.
- Google Groups membership sync (needs Workspace; ADR 0401).
- The Changes API for cheaper incremental listing. Each sync re-reads the tree and every ACL, which is what makes a run "verified" for the small corpus.
