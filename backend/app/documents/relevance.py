"""Relevance-filter study (M3): what the lexical gate drops or keeps wrongly, and how
BGE-M3 dense cosine similarity separates the same pairs.

The pairs (``fixtures/evaluation/relevance-study-m3.toml``) are constructed to probe
failure modes (paraphrases without shared terms, keyword traps), so the rates are
diagnostic, not population estimates. The in-distribution counterpart is the
per-hit gate data that ``retrieval-eval`` records on the real corpus.
"""

from __future__ import annotations

import hashlib
import math
import tomllib
from pathlib import Path
from typing import Any

from app.documents.answers import lexical_keep
from app.reporting.model_client import ModelClient

PAIRS = Path(__file__).resolve().parents[2] / "fixtures" / "evaluation" / "relevance-study-m3.toml"


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def errors(rows: list[dict[str, Any]], keep: Any) -> dict[str, Any]:
    wrong_drop = [r["id"] for r in rows if r["relevant"] and not keep(r)]
    wrong_keep = [r["id"] for r in rows if not r["relevant"] and keep(r)]
    return {
        "relevant_dropped": wrong_drop,
        "irrelevant_kept": wrong_keep,
        "errors": len(wrong_drop) + len(wrong_keep),
    }


def threshold_sweep(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every observed dense score as a candidate threshold (keep score >= t)."""

    candidates = sorted({r["dense_cosine"] for r in rows})
    sweep = []
    for t in candidates:
        e = errors(rows, lambda r, t=t: r["dense_cosine"] >= t)
        sweep.append({"threshold": round(t, 4), **e})
    best = min(sweep, key=lambda s: (s["errors"], -s["threshold"]))
    no_drop = [s for s in sweep if not s["relevant_dropped"]]
    safest = max(no_drop, key=lambda s: s["threshold"]) if no_drop else None
    return {"best": best, "highest_without_relevant_drops": safest, "sweep": sweep}


def run_relevance_study(model: ModelClient) -> dict[str, Any]:
    pairs = tomllib.loads(PAIRS.read_text())["pairs"]
    questions = model.embed_text([p["question"] for p in pairs])
    passages = model.embed_text([p["passage"] for p in pairs])
    rows = []
    for pair, q, d in zip(pairs, questions, passages, strict=True):
        rows.append(
            {
                "id": pair["id"],
                "kind": pair["kind"],
                "relevant": pair["relevant"],
                "lexical_keep": lexical_keep(pair["question"], pair["passage"]),
                "dense_cosine": round(cosine(q.dense, d.dense), 4),
            }
        )
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("paraphrase", "trap", "match", "unrelated"):
        subset = [r for r in rows if r["kind"] == kind]
        scores = [r["dense_cosine"] for r in subset]
        by_kind[kind] = {
            "pairs": len(subset),
            "lexical_keep": sum(1 for r in subset if r["lexical_keep"]),
            "dense_min": min(scores) if scores else None,
            "dense_max": max(scores) if scores else None,
        }
    space = model.embedding_space()
    return {
        "inference_mode": model.mode.value,
        "embedding_space": {
            "model": space.model,
            "revision": space.revision,
            "dense_size": space.dense_size,
            "fake": space.fake,
        },
        "pairs_sha256": hashlib.sha256(PAIRS.read_bytes()).hexdigest(),
        "pair_count": len(rows),
        "by_kind": by_kind,
        "lexical": errors(rows, lambda r: r["lexical_keep"]),
        "dense": threshold_sweep(rows),
        "rows": rows,
    }


def study_markdown(record: dict[str, Any]) -> str:
    kind = "FAKE vectors, not a quality result" if record["embedding_space"]["fake"] else "real"
    lex, dense = record["lexical"], record["dense"]
    lines = [
        f"Relevance study: {record['pair_count']} pairs, embeddings {kind}.",
        "",
        "| Kind | Pairs | Lexical keeps | Dense cosine range |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, k in record["by_kind"].items():
        lines.append(
            f"| {name} | {k['pairs']} | {k['lexical_keep']} |"
            f" {k['dense_min']} to {k['dense_max']} |"
        )
    lines += [
        "",
        f"Lexical gate: drops {len(lex['relevant_dropped'])} relevant"
        f" ({', '.join(lex['relevant_dropped']) or 'none'}), keeps"
        f" {len(lex['irrelevant_kept'])} irrelevant"
        f" ({', '.join(lex['irrelevant_kept']) or 'none'}).",
        f"Best dense threshold {dense['best']['threshold']}: {dense['best']['errors']} errors"
        f" (drops {dense['best']['relevant_dropped'] or 'none'},"
        f" keeps {dense['best']['irrelevant_kept'] or 'none'}).",
    ]
    safest = dense["highest_without_relevant_drops"]
    if safest:
        lines.append(
            f"Highest threshold dropping no relevant pair: {safest['threshold']}"
            f" (keeps {len(safest['irrelevant_kept'])} irrelevant)."
        )
    return "\n".join(lines) + "\n"
