"""Derive the four M7 pilot datasets, deterministically, from seeds and frozen fixtures.

Datasets (roadmap section "M7 evaluation details"; sizes there are targets):
  qa-cases              question-answer cases with atomic claims and cited evidence
  retrieval-pairs       query / chunk-evidence pairs with a relevance label
  checker-train-pairs   (claim, evidence) pairs for checker training (silver labels)
  gold-checker-pairs    (claim, evidence) pairs awaiting two independent human labels

Every record carries its split and family from the frozen manifest and the id of the
seed or legacy fixture it was derived from. Output is sorted and serialised with sorted
keys, so a rebuild is byte-identical; the M7 check rebuilds and compares.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from functools import cache
from pathlib import Path
from typing import Any

from app.evaluation.manifest import Manifest, load_manifest
from app.evaluation.sources import (
    BACKEND,
    FIXTURES,
    M7,
    SourceError,
    all_file_refs,
    canonical_text,
)

PILOT = M7 / "pilot"
SEEDS = PILOT / "seeds.toml"
GENERATED = PILOT / "generated"
INDEX = PILOT / "datasets.json"
DATASETS = ("qa-cases", "retrieval-pairs", "checker-train-pairs", "gold-checker-pairs")
TARGETS = {
    "qa-cases": "100-200",
    "retrieval-pairs": "200-300",
    "checker-train-pairs": "3,000-5,000",
    "gold-checker-pairs": "300-500 (each independently double-labelled)",
}
# The split each dataset is meant for in a release; pilot records outside it are a
# format exercise only (e.g. checker "training" pairs from dev families must not train).
INTENDED_SPLIT = {
    "qa-cases": "heldout",
    "retrieval-pairs": "heldout",
    "checker-train-pairs": "train",
    "gold-checker-pairs": "heldout",
}
M3_CASES = "fixtures/evaluation/documents-dev-m3.toml"
M3_RELEVANCE = "fixtures/evaluation/relevance-study-m3.toml"
AUTHOR_LABEL = "author draft (AI, claude-opus-5-5); not human-reviewed"

Record = dict[str, Any]


@cache
def titles() -> dict[str, str]:
    corpus = tomllib.loads((FIXTURES / "documents" / "corpus.toml").read_text("utf-8"))
    legal = tomllib.loads((FIXTURES / "legal" / "legal.toml").read_text("utf-8"))
    out = {f"doc:{d['key']}": d["title"] for d in corpus["documents"]}
    out.update({f"contract:{c['key']}": c["title"] for c in legal["contracts"]})
    return out


def title_of(ref: str) -> str:
    return titles().get(ref, ref.partition(":")[2])


def _base(manifest: Manifest, dataset: str, rid: str, sources: list[str]) -> Record:
    family = manifest.member_family.get(sources[0], "?") if sources else "?"
    return {
        "id": rid,
        "dataset": dataset,
        "family": family,
        "split": manifest.family_split.get(family, "?"),
        "sources": sources,
        "pilot": True,
        "release": False,
    }


def seed_sources(seeds: dict[str, Any]) -> dict[str, set[str]]:
    """seed id -> every source the seed itself cites (its provenance for derived records)."""

    out: dict[str, set[str]] = {}
    for qa in seeds.get("qa", []):
        refs = set(qa["allowed_sources"]) | {c["source"] for c in qa.get("claims", [])}
        if "family_source" in qa:
            refs.add(qa["family_source"])
        out[qa["id"]] = refs
    for seed in seeds.get("retrieval", []) + seeds.get("claims", []):
        out[seed["id"]] = {seed["source"]}
    return out


def load_seeds() -> dict[str, Any]:
    return tomllib.loads(SEEDS.read_text("utf-8"))


def _sql_value(fixture_case: str, field: str) -> str:
    for path in (
        "fixtures/evaluation/sales-q1-2026.toml",
        "fixtures/evaluation/sales-generalization-m2.toml",
    ):
        data = tomllib.loads((BACKEND / path).read_text("utf-8"))
        for case in data["cases"]:
            if case["id"] == fixture_case:
                return str(case[field])
    raise SourceError(f"unknown SQL fixture case {fixture_case!r}")


def _qa(manifest: Manifest, seeds: dict[str, Any]) -> list[Record]:
    out = []
    for qa in seeds.get("qa", []):
        claims = qa.get("claims", [])
        sources = sorted(
            set(qa["allowed_sources"])
            | {c["source"] for c in claims}
            | ({qa["family_source"]} if "family_source" in qa else set())
        )
        rec = _base(manifest, "qa-cases", qa["id"], sources)
        rec.update(
            question=qa["question"],
            asking_identity=qa["asking_identity"],
            tenant=qa["tenant"],
            expected_kind=qa["expected_kind"],
            allowed_sources=qa["allowed_sources"],
            allowed_side_effects=[],
            derived_from=f"seed:{qa['id']}",
            label_source=AUTHOR_LABEL,
        )
        for key in ("forbidden_values", "note"):
            if key in qa:
                rec[key] = qa[key]
        rec["claims"] = []
        for n, claim in enumerate(claims, start=1):
            item: Record = {
                "id": f"{qa['id']}:c{n}",
                "text": claim["text"],
                "source": claim["source"],
            }
            if "quote" in claim:
                item["evidence"] = {"quote": claim["quote"]}
            else:
                item["evidence"] = {
                    "fixture_case": claim["fixture_case"],
                    "field": claim["field"],
                    "value": _sql_value(claim["fixture_case"], claim["field"]),
                }
            rec["claims"].append(item)
        out.append(rec)
    return out


def locate_passage(passage: str) -> str:
    hits = []
    for ref in all_file_refs():
        try:
            if passage in canonical_text(ref):
                hits.append(ref)
        except Exception:  # noqa: S112 - a source that needs OCR cannot hold the passage
            continue
    if len(hits) != 1:
        raise SourceError(f"relevance passage found in {len(hits)} sources: {passage[:60]!r}")
    return hits[0]


def _retrieval(manifest: Manifest, seeds: dict[str, Any]) -> list[Record]:
    out = []
    m3 = tomllib.loads((BACKEND / M3_CASES).read_text("utf-8"))
    for case in m3["cases"]:
        source = f"doc:{case['document']}"
        rec = _base(manifest, "retrieval-pairs", f"ret-m3-{case['id']}", [source])
        rec.update(
            query=case["question"],
            source=source,
            quote=case["expect"],
            relevant=True,
            derived_from=f"legacy:{M3_CASES}#{case['id']}",
            label_source="frozen M3 dev fixture (2026-11-21); reviewer not recorded",
        )
        out.append(rec)
    rel = tomllib.loads((BACKEND / M3_RELEVANCE).read_text("utf-8"))
    for pair in rel["pairs"]:
        source = locate_passage(pair["passage"])
        rec = _base(manifest, "retrieval-pairs", f"ret-m3rel-{pair['id']}", [source])
        rec.update(
            query=pair["question"],
            source=source,
            quote=pair["passage"],
            relevant=bool(pair["relevant"]),
            note=f"M3 relevance-study kind: {pair['kind']}",
            derived_from=f"legacy:{M3_RELEVANCE}#{pair['id']}",
            label_source="frozen M3 relevance study (2026-11-21), labelled by hand per its "
            "header; reviewer not recorded",
        )
        out.append(rec)
    for seed in seeds.get("retrieval", []):
        rec = _base(manifest, "retrieval-pairs", seed["id"], [seed["source"]])
        rec.update(
            query=seed["query"],
            source=seed["source"],
            quote=seed["quote"],
            relevant=bool(seed["relevant"]),
            derived_from=f"seed:{seed['id']}",
            label_source=AUTHOR_LABEL,
        )
        if "note" in seed:
            rec["note"] = seed["note"]
        out.append(rec)
    return out


def _pair(
    manifest: Manifest,
    dataset: str,
    rid: str,
    seed: Record,
    claim: str,
    kind: str,
    label: str,
    evidence_source: str,
    evidence: str,
) -> Record:
    rec = _base(manifest, dataset, rid, sorted({seed["source"], evidence_source}))
    rec.update(
        claim=claim,
        claim_about=seed["source"],
        evidence_source=evidence_source,
        evidence_title=title_of(evidence_source),
        evidence=evidence,
        kind=kind,
        derived_from=f"seed:{seed['id']}",
    )
    if dataset == "gold-checker-pairs":
        rec["proposed_label"] = label
        rec["label_source"] = (
            "proposed by the AI author only; gold label requires two human labels "
            "(and adjudication on disagreement)"
        )
    else:
        rec["label"] = label
        rec["label_source"] = (
            "rule: authored seed claim or perturbation (silver, not human-reviewed)"
        )
    return rec


def _checker(manifest: Manifest, seeds: dict[str, Any], dataset: str) -> list[Record]:
    seed_set = "checker-train" if dataset == "checker-train-pairs" else "gold-checker"
    chosen = sorted(
        (s for s in seeds.get("claims", []) if s["dataset"] == seed_set), key=lambda s: s["id"]
    )
    out = []
    for i, seed in enumerate(chosen):
        args = (manifest, dataset)
        out.append(
            _pair(
                *args,
                f"{seed['id']}:supported",
                seed,
                seed["claim"],
                "original",
                "supported",
                seed["source"],
                seed["evidence"],
            )
        )
        for n, p in enumerate(seed.get("perturbations", []), start=1):
            out.append(
                _pair(
                    *args,
                    f"{seed['id']}:p{n}-{p['kind']}",
                    seed,
                    p["text"],
                    p["kind"],
                    "unsupported",
                    seed["source"],
                    seed["evidence"],
                )
            )
        if dataset == "checker-train-pairs":
            # Wrong evidence: a seed from another source in the same family, spread over
            # the candidates by position so the evidence spans vary.
            family = manifest.member_family[seed["source"]]
            candidates = [
                o
                for o in chosen
                if o["source"] != seed["source"] and manifest.member_family[o["source"]] == family
            ]
            if candidates:
                other = candidates[i % len(candidates)]
                out.append(
                    _pair(
                        *args,
                        f"{seed['id']}:wrong-evidence",
                        seed,
                        seed["claim"],
                        "wrong_evidence",
                        "unsupported",
                        other["source"],
                        other["evidence"],
                    )
                )
    return out


def _seed_text() -> str:
    return SEEDS.read_text("utf-8")


def build_records(manifest: Manifest | None = None) -> dict[str, list[Record]]:
    if manifest is None:
        manifest, _ = load_manifest()
    seeds = tomllib.loads(_seed_text())
    data = {
        "qa-cases": _qa(manifest, seeds),
        "retrieval-pairs": _retrieval(manifest, seeds),
        "checker-train-pairs": _checker(manifest, seeds, "checker-train-pairs"),
        "gold-checker-pairs": _checker(manifest, seeds, "gold-checker-pairs"),
    }
    for recs in data.values():
        recs.sort(key=lambda r: r["id"])
    return data


def _jsonl(records: list[Record]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)


def render(manifest: Manifest | None = None) -> dict[str, str]:
    """File name -> exact file text, for every generated file and the index."""

    if manifest is None:
        manifest, _ = load_manifest()
    data = build_records(manifest)
    files = {f"generated/{name}.jsonl": _jsonl(recs) for name, recs in data.items()}
    index = {
        "pilot": True,
        "release": False,
        "notice": "PILOT. Not the release evaluation; sizes are far below the roadmap "
        "targets; no label here is human-reviewed unless labels/ says so. Under the active "
        "manifest every current family is development-exposed, so every record is dev: none "
        "may be used for training or as held-out evaluation (see in_intended_split).",
        "manifest": manifest.name,
        "manifest_sha256": manifest.sha256,
        "seeds_sha256": hashlib.sha256(_seed_text().encode("utf-8")).hexdigest(),
        "datasets": {
            name: {
                "file": f"generated/{name}.jsonl",
                "sha256": hashlib.sha256(files[f"generated/{name}.jsonl"].encode()).hexdigest(),
                "count": len(recs),
                "by_split": {s: sum(1 for r in recs if r["split"] == s) for s in manifest.splits},
                "roadmap_target": TARGETS[name],
                "intended_split": INTENDED_SPLIT[name],
                "in_intended_split": sum(1 for r in recs if r["split"] == INTENDED_SPLIT[name]),
            }
            for name, recs in data.items()
        },
    }
    files["datasets.json"] = json.dumps(index, indent=2, sort_keys=True) + "\n"
    return files


def write_pilot() -> dict[Path, int]:
    written: dict[Path, int] = {}
    for rel, text in render().items():
        path = PILOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        written[path] = text.count("\n") if rel.endswith(".jsonl") else 1
    return written


def load_generated() -> dict[str, list[Record]]:
    return {
        name: [
            json.loads(line)
            for line in (GENERATED / f"{name}.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        for name in DATASETS
    }
