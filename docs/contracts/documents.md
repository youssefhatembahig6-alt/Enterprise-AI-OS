# Authorized document answer contract (M3)

Prepared 21 November 2026. Flow: upload → parse → chunk → embed → publish a generation → filtered retrieval → verified answer with exact citations, streamed with the M2 run and event contract ([reports.md](reports.md)). Decisions: [ADR 0301](../adr/0301-document-ingestion-and-generations.md) (ingestion, generations, retries), [ADR 0302](../adr/0302-parser-fallback-and-chunking.md) (parsing and chunking), [ADR 0303](../adr/0303-eligible-id-retrieval.md) (authorization inside retrieval), [ADR 0304](../adr/0304-citations-and-refusals.md) (citations and indistinguishable refusals). Schema: [openapi.json](../openapi.json).

## API

All routes need a session token. Unknown, foreign, deleted and unreadable documents and citations return the same `404 not_found`.

| Route | Who | Purpose |
| --- | --- | --- |
| `POST /api/v1/documents` `{filename, content_base64, department, title?, permissions?}` | `resource.admin` covering `department` | Save the bytes and the permissions, then queue ingestion (`202`). Omitting `permissions` stores the document with `permission_state = unknown`: it is ingested but never searchable |
| `GET /api/v1/documents` | anyone | Searchable documents the caller may read, plus every document the caller administers (with status; metadata only) |
| `GET /api/v1/documents/{id}` | reader or admin | Status, `error {code, message}`, `searchable`, `permission_state`; admins also see versions |
| `PUT /api/v1/documents/{id}/permissions` | admin of the document | Set classification, department restriction, countries and grants (group keys or user emails); marks permissions verified |
| `POST /api/v1/documents/{id}/versions` | admin | Upload a new immutable version; the old version stays searchable until the new one is active |
| `POST /api/v1/documents/{id}/reindex` | admin | New generation of the current version |
| `DELETE /api/v1/documents/{id}` | admin | Tombstone: unsearchable at once; citations stop opening; Qdrant points removed |
| `POST /api/v1/chat` `{question, mode}` | anyone | `mode`: `auto` (default; sales/period questions go to the M2 report, the rest to documents), `documents` or `report` |
| `GET /api/v1/runs/{run}/citations/{cid}` | run owner, current `resource.read` | The cited passage of the exact version used, sliced by Unicode code points on the server |

Upload formats: PDF with a text layer, Markdown, plain text, CSV, XLSX; up to 10 MB. Document statuses: `queued`, `parsing`, `indexing`, `ready`, `failed`, `needs_ocr`. Failure codes include `malformed_file`, `empty_document`, `encoding_error`, `too_large`, `needs_ocr`, `parser_changed`, and while waiting `embedding_unavailable`, `storage_unavailable`, `index_unavailable` (retried, then `failed` with that reason).

## Identity

- A document **is** a protected `resources` row (`kind = document`); its permissions use the M0 five-layer vocabulary.
- Versions are immutable: bytes (SHA-256), canonical text and its SHA-256 never change (database trigger).
- A **generation** is one complete chunking and embedding of a version. Only one per document is `active`; chunks carry their generation.
- Point id: `uuid5(tenant | source | document | version | generation | ordinal)`. The same bytes under another tenant or another document (different permissions) get different ids.
- Qdrant payload: `tenant_id`, `document_id`, `version`, `generation` only. Text and titles stay in PostgreSQL.

## Retrieval

For each question: eligible point ids are computed at policy epoch E0 by the reference evaluator (`resource.read`) over current PostgreSQL state, active generations only. The Qdrant request carries `must: [tenant_id = T, has_id: eligible]` on the dense query, or on each prefetch **and** the RRF fusion stage (`RETRIEVAL_MODE=hybrid`). An empty eligible set sends no query. Candidates are rechecked in a new transaction (current decision, not deleted, generation still active) before chunk text is loaded. A deterministic development relevance gate keeps passages that share at least one significant term with the question.

## Answers and events

Same six SSE events and run states as M2. Status steps: `accepted`, `authorization`, `retrieval`, then `answer_drafting`, `answer_verification`, `publication`. Published content:

- `token` `{message_id, part: "text", index, text, citations: ["c1"]}` per sentence;
- `citation` `{citation_id, document_id, title, version, page, heading_path, char_start, char_end, text_sha256, quote}`;
- `done` with `verification.label = "answer_quotes_verified_dev_checked"`, the checker's model, revision, uncalibrated threshold and scores (gateway mode), or `inference_label` (fake mode).

Every sentence must cite provided sources and quote an exact passage of one cited source. The quote is matched tolerating only whitespace, then mapped to exact code points. Every number in the sentence must appear in the quote. The development NLI checker must score the sentence ≥ 0.5 against its cited passages. At most two repairs; then `verification_failed`.

**No sources.** A run that finds nothing usable, whether nothing relevant exists or the user may not see it, ends with the same events (`status` ×3, `done`) and `abstention = {code: "no_sources", message: "I couldn't find information in documents you can access that answers this."}`. A model that judges the passages insufficient uses the same template. The private reason (`no_eligible_documents`, `no_usable_passages`, `model_unanswerable`) is stored in run stats and the audit log only.

## Citations

Offsets are Unicode code points (start inclusive, end exclusive) in the canonical text of the cited version, which `text_sha256` identifies. Opening a citation returns `before`, `quote` and `after` already sliced on the server, so the browser never converts JavaScript UTF-16 indices. It also returns `version`, `current_version`, `is_current_version` and an `integrity` check (canonical text and quote hashes recomputed). Citations keep opening the version they cite after a new version is published, and stop opening (404) after revocation or deletion.

## Embeddings and fakes

`ModelClient.embed_text` returns BGE-M3 dense (1024) and sparse vectors from the gateway (`/v1/embeddings` with `return_sparse: true`). Collections are named per embedding space, `{prefix}__{model}_{revision}_{size}`, so fake and real vectors never mix. `MODEL_MODE=fake` uses deterministic feature-hashing vectors and a quoting answerer, labelled FAKE on every event. They exercise the pipeline and security tests; they are not retrieval-quality results.
