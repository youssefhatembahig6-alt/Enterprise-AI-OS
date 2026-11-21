"""Retrieval measurement: dense versus dense + sparse (RRF) on the same dev cases.

Uses the production parser, chunker, embedding client and Qdrant query code with the
same tenant + ``has_id`` filter shape (all corpus points eligible, as for a user who
may read everything). Writes one JSON record with the embedding space, model
revisions, Qdrant version, corpus and case fingerprints, per-case ranks and summary
metrics (recall@1, recall@5, MRR@5, document recall@5).

With ``MODEL_MODE=fake`` the vectors are feature-hashing doubles: that run checks the
harness and is labelled ``fake``; it is not a retrieval-quality result. A real result
needs the BGE-M3 gateway (``MODEL_MODE=gateway``).
"""

from __future__ import annotations

import hashlib
import statistics
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

from app.config import RetrievalMode
from app.documents.chunker import CHUNKER_VERSION, chunk_document
from app.documents.parsers import ParseFailed, parse
from app.documents.qdrant import QdrantClient, SparseVector
from app.reporting.model_client import ModelClient

BACKEND = Path(__file__).resolve().parents[2]
CORPUS = BACKEND / "fixtures" / "documents"
CASES = BACKEND / "fixtures" / "evaluation" / "documents-dev-m3.toml"
TENANT = uuid.UUID("00000000-0000-4000-8000-0000000e7a10")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_retrieval_eval(
    model: ModelClient,
    qdrant: QdrantClient,
    *,
    top_k: int = 5,
    prefetch: int = 20,
    tenant: str = "niletech",
) -> dict[str, Any]:
    corpus = tomllib.loads((CORPUS / "corpus.toml").read_text())
    cases = tomllib.loads(CASES.read_text())["cases"]
    space = model.embedding_space()
    collection = f"eval_{uuid.uuid4().hex[:10]}__{space.slug}"
    chunks: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for doc in corpus["documents"]:
        if doc["tenant"] != tenant:
            continue
        try:
            canonical = parse(doc["file"], (CORPUS / "files" / doc["file"]).read_bytes())
        except ParseFailed as exc:
            skipped[doc["key"]] = exc.code
            continue
        for chunk in chunk_document(canonical):
            chunks.append(
                {
                    "id": str(uuid.uuid5(TENANT, f"{doc['key']}|{chunk.ordinal}")),
                    "document": doc["key"],
                    "text": chunk.text,
                }
            )
    started = time.perf_counter()
    vectors = model.embed_text([c["text"] for c in chunks])
    embed_seconds = time.perf_counter() - started
    qdrant.ensure_collection(collection, space.dense_size)
    try:
        qdrant.upsert(
            collection,
            [
                {
                    "id": c["id"],
                    "vector": {
                        "dense": v.dense,
                        "sparse": SparseVector(list(v.sparse), list(v.sparse.values())).as_json(),
                    },
                    "payload": {"tenant_id": str(TENANT), "document_id": c["document"]},
                }
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )
        flt = {
            "must": [
                {"key": "tenant_id", "match": {"value": str(TENANT)}},
                {"has_id": [c["id"] for c in chunks]},
            ]
        }
        by_id = {c["id"]: c for c in chunks}
        results: dict[str, Any] = {}
        for mode in (RetrievalMode.DENSE, RetrievalMode.HYBRID):
            rows = []
            latencies = []
            for case in cases:
                q = model.embed_text([case["question"]])[0]
                t0 = time.perf_counter()
                if mode is RetrievalMode.DENSE:
                    hits = qdrant.query_dense(collection, q.dense, flt, top_k)
                else:
                    hits = qdrant.query_hybrid(
                        collection,
                        q.dense,
                        SparseVector(list(q.sparse), list(q.sparse.values())),
                        flt,
                        top_k,
                        prefetch,
                    )
                latencies.append(time.perf_counter() - t0)
                ranked = [by_id[h.point_id] for h in hits]
                rank = next(
                    (
                        i
                        for i, c in enumerate(ranked, start=1)
                        if c["document"] == case["document"] and case["expect"] in c["text"]
                    ),
                    None,
                )
                doc_rank = next(
                    (i for i, c in enumerate(ranked, start=1) if c["document"] == case["document"]),
                    None,
                )
                rows.append({"id": case["id"], "rank": rank, "document_rank": doc_rank})
            n = len(rows)
            results[mode.value] = {
                "recall_at_1": round(sum(1 for r in rows if r["rank"] == 1) / n, 4),
                "recall_at_5": round(sum(1 for r in rows if r["rank"]) / n, 4),
                "mrr_at_5": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 4),
                "document_recall_at_5": round(sum(1 for r in rows if r["document_rank"]) / n, 4),
                "median_query_ms": round(statistics.median(latencies) * 1000, 2),
                "cases": rows,
            }
    finally:
        qdrant.delete_collection(collection)
    return {
        "inference_mode": model.mode.value,
        "embedding_space": {
            "model": space.model,
            "revision": space.revision,
            "dense_size": space.dense_size,
            "fake": space.fake,
        },
        "model": model.describe(),
        "chunker": CHUNKER_VERSION,
        "corpus_sha256sums": _sha256(CORPUS / "SHA256SUMS"),
        "cases_sha256": _sha256(CASES),
        "case_count": len(cases),
        "chunk_count": len(chunks),
        "skipped_documents": skipped,
        "top_k": top_k,
        "prefetch": prefetch,
        "embed_seconds": round(embed_seconds, 3),
        "results": results,
    }


def markdown_summary(record: dict[str, Any]) -> str:
    space = record["embedding_space"]
    kind = "FAKE feature hashing, not a quality result" if space["fake"] else "real"
    lines = [
        f"Embedding space: `{space['model']}@{space['revision']}` ({kind});"
        f" {record['case_count']} cases, {record['chunk_count']} chunks, top-{record['top_k']}.",
        "",
        "| Mode | recall@1 | recall@5 | MRR@5 | document recall@5 | median query (ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode, r in record["results"].items():
        lines.append(
            f"| {mode} | {r['recall_at_1']:.3f} | {r['recall_at_5']:.3f} | {r['mrr_at_5']:.3f} |"
            f" {r['document_recall_at_5']:.3f} | {r['median_query_ms']:.1f} |"
        )
    return "\n".join(lines) + "\n"
