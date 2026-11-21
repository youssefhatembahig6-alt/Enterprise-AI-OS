"""The retrieval measurement harness runs against real Qdrant and labels fake runs."""

from __future__ import annotations

from app.documents.evaluate import markdown_summary, run_retrieval_eval
from app.documents.qdrant import QdrantClient
from app.reporting.model_client import FakeModelClient


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
