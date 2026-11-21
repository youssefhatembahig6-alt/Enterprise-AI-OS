# ADR 0301 — Document ingestion, generations and safe retries

Status: accepted for M3, pending planning-agent review. Source: specification §9 "Deterministic ingestion", "Safe index publishing", point identity; roadmap "Index publication", "Point identity", "M3 ingestion and retrieval details". Implementation: `backend/app/db/migrations/versions/0005_documents.py`, `backend/app/documents/ingest.py`, `store.py`, `storage.py`, `backend/app/tasks/documents.py`.

## Decision

1. **Permissions before scheduling.** An upload stores the bytes in object storage (SeaweedFS S3 in Compose; tenant-prefixed keys `tenant/source/document/version/file`), then one PostgreSQL transaction creates the protected resource with its permissions, the document, version 1 and an `ingestion_jobs` row. Only after that commit is the job dispatched (Celery, or the inline pool in tests). Without permissions the resource gets `permission_state = 'unknown'` and the most restrictive values. The reference policy denies every content action on it (`deny.unknown_input`) until an administrator sets permissions.
2. **Immutable versions.** Bytes, canonical text and their SHA-256 never change (trigger). A new upload is a new version; re-parsing must reproduce the stored text hash or the job fails `parser_changed`.
3. **Generations.** Each job owns one generation number, recorded on its first claim. Chunk rows and Qdrant points are written under that *staging* generation. Point ids are `uuid5(tenant|source|document|version|generation|ordinal)`, so a retry rewrites the same rows and upserts the same points. The job then checks that PostgreSQL and Qdrant both hold exactly the chunk count, and activates the generation in one transaction. That transaction locks the document, refuses a deleted or superseded document, retires the previous active generation and sets `documents.active_generation`. A partial unique index allows one active generation.
4. **Search sees only active generations** (ADR 0303), so a crash at any step leaves the previous generation searchable and never a mix. Retired and failed generations' points are deleted after activation; eligibility never depended on that cleanup.
5. **Jobs.** Claim with lease; duplicate deliveries are no-ops. A transient failure (embedding gateway, storage, Qdrant) returns the job to pending with a reason (`embedding_unavailable`, ...) shown as the document status, and the beat re-dispatches it after 30 s. After `INGESTION_JOB_MAX_ATTEMPTS` the version fails with that reason, and an administrator can reindex. Deterministic failures (malformed, empty, needs OCR) finish the job with a useful code and message.
6. **Deletion** is a tombstone (`resources.status = 'deleted'`, `documents.deleted_at`), which bumps the policy epoch. A late retry finishes without activating, and the points are removed.
7. **Distinct identities.** Identical bytes uploaded under another tenant or as another document (different permissions) get different resource ids, chunk rows and point ids. No embedding cache is shared across documents.

## Evidence

`tests/integration/test_documents_ingestion.py`:
- permissions observed committed at dispatch time;
- upload authorization;
- statuses for malformed PDF/XLSX, empty file and scan;
- crash after chunks, after upsert and before activation, then retry: one active generation, chunk rows = distinct point ids = Qdrant points, duplicate delivery is a no-op;
- transient embedding failure retried with its reason;
- reindex keeps the old generation searchable until the new one is complete, and cleans up the old points;
- new version, with old citations still opening version 1;
- deletion during ingestion and of an active document;
- identical bytes across tenants and permissions.

## Limits

- Object-store writes happen before the database transaction; a failed transaction leaves an orphan object, which is deleted best-effort.
- PDFs are parsed by Docling on the gateway (ADR 0302), including OCR of scans; without Docling the text-layer fallback applies and scans are `needs_ocr`.
