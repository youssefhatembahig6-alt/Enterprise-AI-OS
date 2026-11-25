"""The deterministic M7 smoke check (no network, no database, no model).

Fails when:
  * the split manifest is malformed, a source is unassigned or in two families, or the
    manifest or any member source changed after freezing;
  * any pre-M7 fixture case or any pilot record sits in a split other than its family's;
  * a record cites sources from more than one family, or a derived record's parent is
    in another family or split;
  * a pilot record uses a sealed held-out family;
  * the same question/claim/query text (digits masked) appears in two splits;
  * an evidence quote is not an exact substring of its source, an SQL claim does not
    state its fixture value, or a claim is not atomic;
  * the generated files differ from a fresh rebuild (non-determinism or hand edits),
    or datasets.json does not point at the frozen manifest;
  * a human label file is malformed.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from typing import Any

from app.evaluation import labels as lab
from app.evaluation.manifest import Manifest, load_lock, load_manifest, verify_lock
from app.evaluation.pilot import (
    DATASETS,
    INDEX,
    PILOT,
    TARGETS,
    Record,
    load_generated,
    locate_passage,
    render,
)
from app.evaluation.sources import BACKEND, SourceError, canonical_text

MAX_CLAIM_WORDS = 30
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


# ------------------------------------------------------------------ split integrity
def legacy_assignments(manifest: Manifest) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(fixture item id, member ref, declared split) for every pre-M7 fixture item."""

    rows: list[tuple[str, str, str]] = []
    problems: list[str] = []
    sql_owner = {
        path: f"sql:{name}"
        for name, tpl in manifest.sql_templates.items()
        for path in tpl["fixtures"]
    }
    for fx in manifest.legacy_fixtures:
        data = tomllib.loads((BACKEND / fx["path"]).read_text("utf-8"))
        declared = fx["split"]
        if fx["kind"] == "sql-cases":
            ref = sql_owner.get(fx["path"])
            if ref is None:
                problems.append(f"{fx['path']}: not owned by any SQL template")
                continue
            for case in data["cases"]:
                if case.get("split", declared) != declared:
                    problems.append(
                        f"{fx['path']}#{case['id']}: case split {case['split']} "
                        f"differs from the fixture's declared {declared}"
                    )
                rows.append((f"{fx['path']}#{case['id']}", ref, declared))
        elif fx["kind"] == "retrieval-cases":
            for case in data["cases"]:
                rows.append((f"{fx['path']}#{case['id']}", f"doc:{case['document']}", declared))
        elif fx["kind"] == "relevance-pairs":
            for pair in data["pairs"]:
                try:
                    ref = locate_passage(pair["passage"])
                except SourceError as exc:
                    problems.append(f"{fx['path']}#{pair['id']}: {exc}")
                    continue
                rows.append((f"{fx['path']}#{pair['id']}", ref, declared))
        else:
            problems.append(f"{fx['path']}: unknown legacy fixture kind {fx['kind']!r}")
    for item, ref, declared in rows:
        family = manifest.member_family.get(ref)
        if family is None:
            problems.append(f"{item}: source {ref} is in no family")
        elif manifest.family_split[family] != declared:
            problems.append(
                f"{item}: family {family} is {manifest.family_split[family]} but the fixture "
                f"is {declared} — the family would cross splits"
            )
    return rows, problems


def _claim_texts(rec: Record) -> list[str]:
    texts = [rec[k] for k in ("question", "query", "claim") if k in rec]
    texts += [c["text"] for c in rec.get("claims", [])]
    return texts


def normalise(text: str) -> str:
    return _SPACE.sub(" ", _DIGITS.sub("#", text.lower())).strip()


def check_records(manifest: Manifest, data: dict[str, list[Record]]) -> list[str]:
    problems: list[str] = []
    sealed = manifest.sealed()
    seen_ids: dict[str, str] = {}
    seed_family: dict[str, str] = {}
    text_splits: dict[str, set[str]] = defaultdict(set)
    for name, records in data.items():
        for rec in records:
            rid = rec["id"]
            where = f"{name}:{rid}"
            if rid in seen_ids:
                problems.append(f"{where}: id also used in {seen_ids[rid]}")
            seen_ids[rid] = name
            if not rec.get("pilot") or rec.get("release") is not False:
                problems.append(f"{where}: pilot records must be pilot=true, release=false")
            families = {manifest.member_family.get(s) for s in rec["sources"]}
            if None in families:
                problems.append(f"{where}: cites a source in no family {rec['sources']}")
                continue
            if len(families) != 1:
                problems.append(
                    f"{where}: cites sources from several families {sorted(map(str, families))}"
                )
            family = rec["family"]
            if family not in families:
                problems.append(f"{where}: family {family} does not own its sources")
                continue
            if rec["split"] != manifest.family_split[family]:
                problems.append(
                    f"{where}: split {rec['split']} but family {family} is "
                    f"{manifest.family_split[family]} — crosses splits"
                )
            if family in sealed:
                problems.append(f"{where}: uses sealed held-out family {family}")
            parent = rec.get("derived_from", "")
            if parent.startswith("seed:"):
                if seed_family.setdefault(parent, family) != family:
                    problems.append(
                        f"{where}: derived from {parent}, which is in family "
                        f"{seed_family[parent]} — derived example crosses families"
                    )
            elif not parent.startswith("legacy:"):
                problems.append(f"{where}: derived_from must name a seed or legacy item")
            for text in _claim_texts(rec):
                text_splits[normalise(text)].add(rec["split"])
            problems += _check_evidence(where, rec)
    # Legacy items participate in the cross-split text check through their questions.
    for fx in manifest.legacy_fixtures:
        fdata = tomllib.loads((BACKEND / fx["path"]).read_text("utf-8"))
        for item in fdata.get("cases", []) + fdata.get("pairs", []):
            text_splits[normalise(item["question"])].add(fx["split"])
    for text, splits in sorted(text_splits.items()):
        if len(splits) > 1:
            problems.append(f"text appears in splits {sorted(splits)}: {text[:80]!r}")
    return problems


def _check_evidence(where: str, rec: Record) -> list[str]:
    problems: list[str] = []
    quotes: list[tuple[str, str]] = []
    if "quote" in rec:
        quotes.append((rec["source"], rec["quote"]))
    if "evidence" in rec and isinstance(rec["evidence"], str):
        quotes.append((rec["evidence_source"], rec["evidence"]))
        problems += _atomic(where, rec["claim"])
    for claim in rec.get("claims", []):
        problems += _atomic(f"{where}/{claim['id']}", claim["text"])
        ev = claim["evidence"]
        if "quote" in ev:
            quotes.append((claim["source"], ev["quote"]))
        else:
            numbers = re.findall(r"-?[\d,]*\d(?:\.\d+)?", claim["text"])
            stated = {t.replace(",", "") for t in numbers}
            if ev["value"] not in stated:
                problems.append(
                    f"{where}/{claim['id']}: claim does not state fixture value {ev['value']}"
                )
    if rec.get("expected_kind") == "answer" and not rec.get("claims"):
        problems.append(f"{where}: an answer case needs at least one atomic claim")
    if rec.get("expected_kind") == "abstention" and rec.get("claims"):
        problems.append(f"{where}: an abstention case must not carry claims")
    for source, quote in quotes:
        try:
            if quote not in canonical_text(source):
                problems.append(
                    f"{where}: evidence is not an exact span of {source}: {quote[:60]!r}"
                )
        except (SourceError, ValueError) as exc:
            problems.append(f"{where}: {exc}")
    return problems


def _atomic(where: str, text: str) -> list[str]:
    problems = []
    body = text.strip()
    if ";" in body:
        problems.append(f"{where}: claim has a semicolon (split it into atomic claims)")
    if re.search(r"[.!?]\s+[A-Z]", body):
        problems.append(f"{where}: claim has more than one sentence")
    if len(body.split()) > MAX_CLAIM_WORDS:
        problems.append(f"{where}: claim exceeds {MAX_CLAIM_WORDS} words")
    return problems


# ------------------------------------------------------------------------ main check
def run_checks() -> tuple[list[str], str]:
    manifest, problems = load_manifest()
    problems = [f"manifest: {p}" for p in problems]
    problems += [f"freeze: {p}" for p in verify_lock(manifest, load_lock())]
    legacy, legacy_problems = legacy_assignments(manifest)
    problems += [f"legacy: {p}" for p in legacy_problems]
    expected = render(manifest)
    for rel, text in sorted(expected.items()):
        path = PILOT / rel
        if not path.exists() or path.read_text("utf-8") != text:
            problems.append(
                f"determinism: {rel} differs from a fresh build "
                "(run `python -m app.evaluation build-pilot` and review the diff)"
            )
    index = json.loads(INDEX.read_text("utf-8")) if INDEX.exists() else {}
    if index.get("manifest_sha256") != manifest.sha256:
        problems.append("datasets.json was not generated from the frozen manifest")
    data = load_generated()
    problems += [f"records: {p}" for p in check_records(manifest, data)]
    label_status = lab.status()
    problems += [f"labels: {p}" for p in label_status["problems"]]
    return problems, format_report(counts_report(manifest, data, legacy, label_status))


def counts_report(
    manifest: Manifest | None = None,
    data: dict[str, list[Record]] | None = None,
    legacy: list[tuple[str, str, str]] | None = None,
    label_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if manifest is None:
        manifest, _ = load_manifest()
    data = data if data is not None else load_generated()
    if legacy is None:
        legacy, _ = legacy_assignments(manifest)
    label_status = label_status if label_status is not None else lab.status()
    fam_counts: dict[str, int] = defaultdict(int)
    for fam in manifest.family_split.values():
        fam_counts[fam] += 1
    return {
        "manifest": {
            "name": manifest.name,
            "sha256": manifest.sha256,
            "families_by_split": dict(sorted(fam_counts.items())),
            "sealed_families": sorted(manifest.sealed()),
        },
        "legacy_items_checked": len(legacy),
        "datasets": {
            name: {
                "count": len(data[name]),
                "by_split": {
                    s: sum(1 for r in data[name] if r["split"] == s) for s in manifest.splits
                },
                "roadmap_target": TARGETS[name],
            }
            for name in DATASETS
        },
        "qa_claims": sum(len(r.get("claims", [])) for r in data["qa-cases"]),
        "gold_labels": {k: v for k, v in label_status.items() if k != "problems"},
    }


def format_report(report: dict[str, Any]) -> str:
    m = report["manifest"]
    lines = [
        f"Split manifest {m['name']} sha256={m['sha256'][:16]}…  families by split: "
        f"{m['families_by_split']}  sealed: {m['sealed_families']}",
        f"Pre-M7 fixture items mapped to families: {report['legacy_items_checked']}",
        "",
        "PILOT datasets (not the release evaluation; targets are roadmap plans):",
        f"  {'dataset':<22}{'count':>6}  {'train':>5} {'dev':>5} {'heldout':>7}  target",
    ]
    for name, d in report["datasets"].items():
        s = d["by_split"]
        lines.append(
            f"  {name:<22}{d['count']:>6}  {s['train']:>5} {s['dev']:>5} "
            f"{s['heldout']:>7}  {d['roadmap_target']}"
        )
    lines.append(f"  qa-cases atomic claims: {report['qa_claims']}")
    g = report["gold_labels"]
    kappa = "n/a" if g["cohen_kappa"] is None else g["cohen_kappa"]
    lines += [
        "",
        f"Human review of gold checker pairs: {len(g['human_reviewed_pair_ids'])} of "
        f"{g['pairs']} pairs have any human label; {g['double_labelled']} double-labelled; "
        f"{len(g['disagreements'])} disagreements ({g['adjudicated']} adjudicated); "
        f"gold labels: {g['gold_labels']}; Cohen's kappa: {kappa}",
    ]
    return "\n".join(lines)
