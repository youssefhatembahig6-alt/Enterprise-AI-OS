"""Relevance gate options, the settings guard and the relevance study harness."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import RelevanceFilter, Settings
from app.documents.answers import relevance_filter
from app.documents.relevance import run_relevance_study, threshold_sweep
from app.reporting.model_client import FakeModelClient

BASE = {
    "DATABASE_URL": "postgresql+psycopg://x@localhost/none",
    "REDIS_URL": "redis://localhost:6379/0",
    "QDRANT_URL": "http://localhost:6333",
    "S3_ENDPOINT_URL": "http://localhost:8333",
    "JWT_SECRET": "unit-test-secret-unit-test-secret-0000",
}


def passage(text: str, score: float) -> dict[str, Any]:
    return {"prefix": "", "span_text": text, "score": score}


PASSAGES = [
    passage("Employees in Egypt receive 21 working days of annual leave.", 0.71),
    passage("Report a lost or stolen laptop within one hour.", 0.22),
]


def test_lexical_gate_keeps_only_passages_sharing_a_term() -> None:
    kept = relevance_filter(RelevanceFilter.LEXICAL, 0.0, "How much leave in Egypt?", PASSAGES)
    assert kept == PASSAGES[:1]
    assert relevance_filter(RelevanceFilter.LEXICAL, 0.0, "Vacation in Cairo?", PASSAGES) == []


def test_dense_score_gate_uses_the_hit_score_and_none_keeps_everything() -> None:
    assert relevance_filter(RelevanceFilter.DENSE_SCORE, 0.5, "anything", PASSAGES) == PASSAGES[:1]
    assert relevance_filter(RelevanceFilter.DENSE_SCORE, 0.1, "anything", PASSAGES) == PASSAGES
    assert relevance_filter(RelevanceFilter.NONE, 0.9, "anything", PASSAGES) == PASSAGES


def test_dense_score_gate_requires_dense_retrieval() -> None:
    Settings(**BASE, RELEVANCE_FILTER="dense_score", RETRIEVAL_MODE="dense")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="requires RETRIEVAL_MODE=dense"):
        Settings(**BASE, RELEVANCE_FILTER="dense_score", RETRIEVAL_MODE="hybrid")  # type: ignore[arg-type]


def test_threshold_sweep_reports_best_and_safest_thresholds() -> None:
    rows = [
        {"id": "a", "relevant": True, "dense_cosine": 0.8},
        {"id": "b", "relevant": True, "dense_cosine": 0.4},
        {"id": "c", "relevant": False, "dense_cosine": 0.5},
        {"id": "d", "relevant": False, "dense_cosine": 0.1},
    ]
    sweep = threshold_sweep(rows)
    assert sweep["best"]["errors"] == 1
    assert sweep["highest_without_relevant_drops"]["threshold"] == 0.4
    assert sweep["highest_without_relevant_drops"]["irrelevant_kept"] == ["c"]


def test_relevance_study_records_lexical_failures_and_labels_fake_vectors() -> None:
    record = run_relevance_study(FakeModelClient())
    assert record["embedding_space"]["fake"] is True and record["inference_mode"] == "fake"
    assert record["pair_count"] == 24
    # The pairs are built to expose the word-overlap gate: every paraphrase is dropped
    # and every keyword trap is kept.
    assert record["by_kind"]["paraphrase"]["lexical_keep"] == 0
    assert record["by_kind"]["trap"]["lexical_keep"] == record["by_kind"]["trap"]["pairs"]
    assert len(record["lexical"]["relevant_dropped"]) == 8
