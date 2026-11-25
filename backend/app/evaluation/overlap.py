"""Overlap checks for the fresh v3 corpus against the dev corpus and between families.

Two measures:
  * template overlap: shared word 8-grams after lower-casing and masking digits, so two
    documents built from one template collide even when their numbers differ;
  * fact overlap: every proper name in a fresh family's facts (suppliers, sites, clients,
    services, ...) must not occur in any dev text or in a fresh family of another split.

Dev texts are the canonical text of every dev family member (the application's own
parser), the pre-M7 evaluation fixtures and the pilot seeds. If the original generator's
archive is present locally (../reference-data/niletech-0.1.6/documents), its documents
are compared too. CI does not have the archive, so that comparison is reported as skipped.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from itertools import combinations
from typing import Any

from app.documents.parsers import ParseFailed
from app.evaluation.manifest import REPO, Manifest
from app.evaluation.sources import BACKEND, M7, SourceError, canonical_text

SHINGLE = 8
_WORD = re.compile(r"\w+", re.UNICODE)
ARCHIVE = REPO.parent / "reference-data" / "niletech-0.1.6" / "documents"
FACT_NAME_KEYS = (
    "site",
    "vendor",
    "supplier",
    "client",
    "service",
    "asset",
    "item",
    "awarded_to",
    "city",
)


def shingles(text: str, n: int = SHINGLE) -> set[str]:
    words = [re.sub(r"\d", "#", w) for w in _WORD.findall(text.lower())]
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def _dev_texts(manifest: Manifest) -> dict[str, str]:
    out: dict[str, str] = {}
    for ref, fam in sorted(manifest.member_family.items()):
        if manifest.family_split[fam] != "dev" or ref.startswith("sql:"):
            continue
        try:
            out[ref] = canonical_text(ref)
        except (SourceError, ValueError, ParseFailed):
            continue  # a scan that needs OCR has no native text (it is still dev)
    for fx in manifest.legacy_fixtures:
        out[f"fixture:{fx['path']}"] = (BACKEND / fx["path"]).read_text("utf-8")
    out["fixture:pilot/seeds.toml"] = (M7 / "pilot" / "seeds.toml").read_text("utf-8")
    return out


def _archive_texts() -> dict[str, str] | None:
    if not ARCHIVE.is_dir():
        return None
    return {
        f"archive:{p.relative_to(ARCHIVE).as_posix()}": p.read_text("utf-8", errors="replace")
        for p in sorted(ARCHIVE.rglob("*.md"))
    }


def _fresh_docs(manifest: Manifest) -> dict[str, tuple[str, str, str]]:
    """ref -> (family, split, text) for every fresh member."""

    return {
        ref: (fam, manifest.family_split[fam], canonical_text(ref))
        for ref, fam in sorted(manifest.member_family.items())
        if ref.startswith("fresh:")
    }


def _fact_names(ref: str) -> set[str]:
    corpus = M7 / "corpus-v3"
    key = ref.partition(":")[2]
    facts: dict[str, Any] = json.loads((corpus / "facts" / f"{key}.json").read_text("utf-8"))
    names = {str(facts[k]) for k in FACT_NAME_KEYS if k in facts}
    names |= {q["supplier"] for q in facts.get("quotes", [])}
    return names


def overlap_report(manifest: Manifest) -> dict[str, Any]:
    fresh = _fresh_docs(manifest)
    dev = _dev_texts(manifest)
    archive = _archive_texts()
    dev_shingles: dict[str, set[str]] = {k: shingles(v) for k, v in dev.items()}
    archive_shingles = {k: shingles(v) for k, v in (archive or {}).items()}
    dev_lower = {k: v.lower() for k, v in dev.items()}

    fam_shingles: dict[str, set[str]] = defaultdict(set)
    fam_names: dict[str, set[str]] = defaultdict(set)
    fam_split: dict[str, str] = {}
    fam_text: dict[str, str] = defaultdict(str)
    for ref, (fam, split, text) in fresh.items():
        fam_shingles[fam] |= shingles(text)
        fam_names[fam] |= _fact_names(ref)
        fam_split[fam] = split
        fam_text[fam] += "\n" + text.lower()

    def hits(sh: set[str], pool: dict[str, set[str]]) -> dict[str, int]:
        return {k: len(sh & s) for k, s in pool.items() if sh & s}

    families: dict[str, Any] = {}
    for fam in sorted(fam_shingles):
        families[fam] = {
            "split": fam_split[fam],
            "documents": sum(1 for f, _, _ in fresh.values() if f == fam),
            "shingles": len(fam_shingles[fam]),
            "shared_with_dev": hits(fam_shingles[fam], dev_shingles),
            "shared_with_original_archive": (
                None if archive is None else hits(fam_shingles[fam], archive_shingles)
            ),
            "names_in_dev": sorted(
                n for n in fam_names[fam] if any(n.lower() in t for t in dev_lower.values())
            ),
        }
    pairs = []
    for a, b in combinations(sorted(fam_shingles), 2):
        shared = fam_shingles[a] & fam_shingles[b]
        names = sorted(n for n in fam_names[a] if n.lower() in fam_text[b]) + sorted(
            n for n in fam_names[b] if n.lower() in fam_text[a]
        )
        pairs.append(
            {
                "families": [a, b],
                "splits": [fam_split[a], fam_split[b]],
                "shared_shingles": len(shared),
                "shared_names": names,
            }
        )
    return {
        "shingle_words": SHINGLE,
        "dev_texts_compared": len(dev),
        "original_archive_documents_compared": None if archive is None else len(archive),
        "families": families,
        "family_pairs": pairs,
    }


def overlap_problems(report: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for fam, r in report["families"].items():
        if r["shared_with_dev"]:
            problems.append(f"{fam} shares template text with dev sources {r['shared_with_dev']}")
        if r["shared_with_original_archive"]:
            problems.append(
                f"{fam} shares template text with the original generator's documents "
                f"{sorted(r['shared_with_original_archive'])[:3]}"
            )
        if r["names_in_dev"]:
            problems.append(f"{fam} reuses names found in dev sources: {r['names_in_dev']}")
    for p in report["family_pairs"]:
        a, b = p["families"]
        if p["shared_shingles"]:
            problems.append(f"{a} and {b} share {p['shared_shingles']} template 8-grams")
        if p["shared_names"]:
            problems.append(f"{a} and {b} share fact names {p['shared_names']}")
    return problems


def load_corpus_index() -> dict[str, Any]:
    path = M7 / "corpus-v3" / "corpus.toml"
    return tomllib.loads(path.read_text("utf-8")) if path.is_file() else {}
