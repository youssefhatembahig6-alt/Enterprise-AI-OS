# ADR 0401 — Google Drive sync and permission freshness

Status: Accepted for M4 (pending planning review). Relates to: FR-10, FR-02, roadmap "M4 and M5 connector and UI details", ADR 0301, ADR 0303.

## Context

Drive is an external system of record for sharing. The application can only know Drive's permissions as of its last read, and Drive offers no push revocation the laptop stack could rely on. The roadmap requires full, incremental, move, deletion and ACL sync, denial when permissions cannot be established, a 15-minute demo interval and a nightly interval otherwise, and a visible last verified sync time without promising immediate external revocation.

## Decision

1. **A source is a Drive folder** bound to one tenant and one owning department. The admin sets the attributes Drive does not carry (department, classification, department restriction, countries). Drive sharing supplies only layer 4 (resource grant) of the reference policy; the other four layers are unchanged.
2. **Every sync re-reads every file's permission list.** A run walks the folder tree breadth-first and reads each file's permissions. Content is downloaded only when the file's fingerprint changed (`md5Checksum`, or `modifiedTime` for Google-native files), so unchanged files are not re-ingested and an unchanged ACL does not rewrite grants (no policy-epoch churn). New files and new content use the M3 ingestion path unchanged (permissions saved before the job exists, staged generations, tombstones).
3. **Mapping.** A `user` permission grants the tenant user with that exact email. A `group` permission grants an internal group only through an explicit admin link (`external_group_links`). `domain` and "anyone with the link" grant nobody. Unmapped principals are recorded per item for the admin. Every Drive role from `reader` up implies read; Drive has no deny.
4. **Fail closed, three ways.**
   - The root or any folder cannot be listed, or credentials fail → the run fails, `permissions_current = false`, and **every** item of the source is denied until a later run succeeds.
   - One file's permission list cannot be read (403/404 or no list returned) → that item's resource becomes `permission_state = unknown` (denied); the rest of the source stays current.
   - Readers deny connector content once `last_verified_sync_at` is older than the tenant's `max_staleness_seconds` (30 minutes in demo mode, 30 hours nightly), whether or not a sync ever reported failure. This covers a stopped scheduler.
   A paused or deleted source, or a tenant that disabled the `google_drive` connector, also denies its content.
5. **One enforcement point.** `load_resource_meta(s)` computes `permissions_current` in SQL from the resource, its source and the tenant settings. Eligibility (search), `can_read`, publication rechecks, citation reopen and replay all go through it, so no path can serve Drive content the freshness rule denies.
6. **Verified time is conservative.** `last_verified_sync_at` is the run's *start* time: every permission was read after it.
7. **Moves and removals.** A rename or move inside the tree updates title and path and keeps the document. A file trashed, deleted or moved out of the tree is tombstoned (M3), and its Qdrant points are removed. If it reappears it becomes a new document; a tombstone is never resurrected.
8. **Schedule.** Celery beat asks PostgreSQL every minute which sources are due (`connector_sources_due`): demo — 15 minutes after the last start; nightly — once per day after 02:00 UTC. A lease on the source row makes duplicate deliveries no-ops. Admins can also sync on demand.
9. **Credentials are the operator's.** `DRIVE_CONNECTOR_CONFIG` binds each tenant slug to server-side credentials (service account key file or OAuth refresh token, `drive.readonly`) and to the root folders that tenant may use. A tenant admin cannot point a source at a folder outside that list, so one deployment credential cannot leak another tenant's folder.

## Consequences

- External revocation is effective **after the next verified sync** (≤15 minutes in demo mode), and within the staleness bound at worst. The admin console shows the last verified sync time and the current access state of every source.
- A transient Drive outage denies a whole source until the next successful run. This is deliberate.
- Group membership comes from the internal group, not from Google Groups: consumer Google accounts have no API for group membership, and the Workspace Directory API is not available to the project. A Drive group permission without a link grants nobody.
- The full permission re-read costs one API call per file per run. That is acceptable for the project's small corpus and is what makes a run "verified". The Changes API could later reduce downloads further, but it cannot replace the full ACL read without weakening the verification claim.
