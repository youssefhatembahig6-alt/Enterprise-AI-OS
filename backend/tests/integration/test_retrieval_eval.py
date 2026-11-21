"""The retrieval measurement harness runs against real Qdrant and labels fake runs."""

from __future__ import annotations

import uuid
from typing import Any

from app.documents.evaluate import markdown_summary, run_retrieval_eval
from app.documents.qdrant import QdrantClient
from app.reporting.model_client import FakeModelClient


def test_concurrent_collection_creation_is_idempotent(qdrant_url: str) -> None:
    name = f"test_{uuid.uuid4().hex}"
    winner = QdrantClient(qdrant_url)

    def create_before_put(method: str, path: str, body: dict[str, Any] | None) -> None:
        if method == "PUT" and path == f"/collections/{name}":
            winner.ensure_collection(name, 256)

    other = QdrantClient(qdrant_url, on_request=create_before_put)
    try:
        other.ensure_collection(name, 256)
        assert name in qdrant_collections(qdrant_url)
    finally:
        winner.delete_collection(name)
        winner.close()
        other.close()


def test_harness_compares_dense_and_hybrid_on_the_same_cases(qdrant_url: str) -> None:
    qdrant = QdrantClient(qdrant_url, timeout=60.0)
    try:
        record = run_retrieval_eval(FakeModelClient(), qdrant)
    finally:
        qdrant.close()
    assert record["inference_mode"] == "fake" and record["embedding_space"]["fake"] is True
    assert set(record["results"]) == {"dense", "hybrid"}
    assert record["skipped_documents"] == {"delivery-note-scan": "needs_ocr"}
    for result in record["results"].values():
        assert len(result["cases"]) == record["case_count"] == 22
        assert 0.0 <= result["recall_at_1"] <= result["recall_at_5"] <= 1.0
    assert "FAKE" in markdown_summary(record)
    leftovers = [c for c in qdrant_collections(qdrant_url) if c.startswith("eval_")]
    assert leftovers == []


def qdrant_collections(url: str) -> list[str]:
    import httpx

    return [c["name"] for c in httpx.get(f"{url}/collections").json()["result"]["collections"]]
