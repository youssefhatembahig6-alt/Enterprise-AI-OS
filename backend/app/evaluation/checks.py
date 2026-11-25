"""The deterministic M7 smoke check (no network, no database, no model).

Fails when:
  * a manifest or lock differs from the reviewed snapshot, a registered (also superseded)
    manifest no longer verifies against its lock, or a member source changed;
  * with ``--base``, a manifest, lock or snapshot entry frozen on the base branch changed;
    with ``--git-history``, the active freeze was not committed before the pilot;
  * the active manifest is malformed, a source is unassigned or in two families, or a
    development-exposed family is outside dev (automatic scan of tests, scripts, ML runs
    and evidence captures);
  * a pre-M7 fixture item or a pilot record is outside its family's split;
  * a record cites an evidence source it does not declare, a source in no family, or
    sources from several families;
  * a record's parent (seed or legacy fixture item) is unknown or in another family/split;
  * a pilot record uses a sealed held-out family;
  * the same question/claim/query text (digits masked) appears in two splits;
  * an evidence quote is not an exact substring of its source, an SQL claim does not
    state its fixture value, or a claim is not atomic;
  * the generated files differ from a fresh rebuild, or a label file is malformed.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from app.evaluation import corpus_v3, freeze, overlap
from app.evaluation import labels as lab
from app.evaluation.manifest import Manifest
from app.evaluation.pilot import (
    DATASETS,
    INTENDED_SPLIT,
    TARGETS,
    Record,
    load_seeds,
    locate_passage,
    render,
    seed_sources,
)
from app.evaluation.sources import BACKEND, SourceError, canonical_text

MAX_CLAIM_WORDS = 30
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")

LegacyIndex = dict[str, tuple[str, str]]  # "path#id" -> (member ref, declared split)


# ------------------------------------------------------------------ legacy fixtures
def legacy_assignments(manifest: Manifest) -> tuple[LegacyIndex, list[str]]:
    """Every pre-M7 fixture item -> (member ref, declared split), plus problems."""

    rows: LegacyIndex = {}
    problems: list[str] = []
    sql_owner = {
        path: f"sql:{name}"
        for name, tpl in manifest.sql_templates.items()
        for path in tpl["fixtures"]
    }
    for fx in manifest.legacy_fixtures:
        data = tomllib.loads((BACKEND / fx["path"]).read_text("utf-8"))
        declared, path = fx["split"], fx["path"]
        if fx["kind"] == "sql-cases":
            ref = sql_owner.get(path)
            if ref is None:
                problems.append(f"{path}: not owned by any SQL template")
                continue
            for case in data["cases"]:
                if case.get("split", declared) != declared:
                    problems.append(
                        f"{path}#{case['id']}: case split {case['split']} differs from the "
                        f"fixture's declared {declared}"
                    )
                rows[f"{path}#{case['id']}"] = (ref, declared)
        elif fx["kind"] == "retrieval-cases":
            for case in data["cases"]:
                rows[f"{path}#{case['id']}"] = (f"doc:{case['document']}", declared)
        elif fx["kind"] == "relevance-pairs":
            for pair in data["pairs"]:
                try:
                    rows[f"{path}#{pair['id']}"] = (locate_passage(pair["passage"]), declared)
                except SourceError as exc:
                    problems.append(f"{path}#{pair['id']}: {exc}")
        else:
            problems.append(f"{path}: unknown legacy fixture kind {fx['kind']!r}")
    for item, (ref, declared) in rows.items():
        family = manifest.member_family.get(ref)
        if family is None:
            problems.append(f"{item}: source {ref} is in no family")
        elif manifest.family_split[family] != declared:
            problems.append(
                f"{item}: family {family} is {manifest.family_split[family]} but the fixture "
                f"is {declared}, so the family would cross splits"
            )
    return rows, problems


# ------------------------------------------------------------------- fresh corpus
def corpus_assignment_problems(manifest: Manifest) -> list[str]:
    """The generator's declared family/split must equal the frozen manifest's, document by
    document, and every fresh family must be assigned whole."""

    problems: list[str] = []
    index = overlap.load_corpus_index()
    for doc in index.get("documents", []):
        ref = f"fresh:{doc['key']}"
        fam = manifest.member_family.get(ref)
        if fam is None:
            problems.append(f"{ref} is not assigned by {manifest.name}")
        elif fam != doc["family"] or manifest.family_split[fam] != doc["split"]:
            problems.append(
                f"{ref}: generated as {doc['family']} ({doc['split']}) but the manifest has "
                f"{fam} ({manifest.family_split[fam]})"
            )
    by_family: dict[str, set[str]] = defaultdict(set)
    for doc in index.get("documents", []):
        by_family[doc["family"]].add(doc["template"])
    for fam, templates in sorted(by_family.items()):
        if len(templates) != 1:
            problems.append(f"fresh family {fam} mixes templates {sorted(templates)}")
    owners: dict[str, set[str]] = defaultdict(set)
    for doc in index.get("documents", []):
        owners[doc["template"]].add(doc["family"])
    for tpl, fams in sorted(owners.items()):
        if len(fams) != 1:
            problems.append(f"template {tpl} is used by several families {sorted(fams)}")
    return problems


# ------------------------------------------------------------------------- records
def evidence_refs(rec: Record) -> set[str]:
    """Every source a record actually uses as evidence or scope, wherever it is stored."""

    refs = {rec[k] for k in ("source", "evidence_source", "claim_about") if k in rec}
    refs |= set(rec.get("allowed_sources", []))
    refs |= {c["source"] for c in rec.get("claims", [])}
    return refs


def _claim_texts(rec: Record) -> list[str]:
    texts = [rec[k] for k in ("question", "query", "claim") if k in rec]
    texts += [c["text"] for c in rec.get("claims", [])]
    return texts


def normalise(text: str) -> str:
    return _SPACE.sub(" ", _DIGITS.sub("#", text.lower())).strip()


def _parent_family(
    manifest: Manifest, parent: str, seeds: dict[str, set[str]], legacy: LegacyIndex
) -> tuple[str | None, str | None, str | None]:
    """(family, split, error) for a record's ``derived_from``."""

    kind, _, ident = parent.partition(":")
    if kind == "seed":
        if ident not in seeds:
            return None, None, f"unknown parent {parent} (no such seed)"
        fams = {manifest.member_family.get(r) for r in seeds[ident]}
        fam = next(iter(fams)) if len(fams) == 1 else None
        if fam is None:
            return None, None, f"parent {parent} cites sources outside one family"
        return fam, manifest.family_split[fam], None
    if kind == "legacy":
        path = ident.partition("#")[0]
        if path not in {fx["path"] for fx in manifest.legacy_fixtures}:
            return None, None, f"unknown parent {parent} (fixture not in the manifest)"
        if ident not in legacy:
            return None, None, f"unknown parent {parent} (no such fixture item)"
        ref, declared = legacy[ident]
        fam = manifest.member_family.get(ref)
        if fam is None:
            return None, None, f"parent {parent} uses {ref}, which is in no family"
        return fam, declared, None
    return None, None, f"derived_from must be seed:<id> or legacy:<path>#<id>, got {parent!r}"


def check_records(
    manifest: Manifest,
    data: Mapping[str, list[Record]],
    seeds: dict[str, Any] | None = None,
    legacy: LegacyIndex | None = None,
) -> list[str]:
    seed_refs = seed_sources(seeds if seeds is not None else load_seeds())
    if legacy is None:
        legacy, _ = legacy_assignments(manifest)
    problems: list[str] = []
    sealed = manifest.sealed()
    seen_ids: dict[str, str] = {}
    text_splits: dict[str, set[str]] = defaultdict(set)
    for name, records in data.items():
        for rec in records:
            where = f"{name}:{rec['id']}"
            if rec["id"] in seen_ids:
                problems.append(f"{where}: id also used in {seen_ids[rec['id']]}")
            seen_ids[rec["id"]] = name
            if not rec.get("pilot") or rec.get("release") is not False:
                problems.append(f"{where}: pilot records must be pilot=true, release=false")
            declared = set(rec.get("sources", []))
            used = evidence_refs(rec)
            for ref in sorted(used - declared):
                problems.append(f"{where}: uses evidence from {ref}, which it does not declare")
            for ref in sorted(r for r in declared | used if r not in manifest.member_family):
                problems.append(f"{where}: source {ref} is not in the manifest")
            families = {
                manifest.member_family[r] for r in declared | used if r in manifest.member_family
            }
            if len(families) > 1:
                problems.append(
                    f"{where}: cites sources from several families {sorted(families)} "
                    f"(splits {sorted({manifest.family_split[f] for f in families})})"
                )
            family = rec["family"]
            if family not in manifest.family_split:
                problems.append(f"{where}: family {family} is not in the manifest")
                continue
            if families and family not in families:
                problems.append(f"{where}: family {family} does not own its sources")
            split = manifest.family_split[family]
            if rec["split"] != split:
                problems.append(
                    f"{where}: split {rec['split']} but family {family} is {split}, "
                    "so the record crosses splits"
                )
            if family in sealed:
                problems.append(f"{where}: uses sealed held-out family {family}")
            p_fam, p_split, err = _parent_family(
                manifest, rec.get("derived_from", ""), seed_refs, legacy
            )
            if err:
                problems.append(f"{where}: {err}")
            elif p_fam != family or p_split != rec["split"]:
                problems.append(
                    f"{where}: derived from {rec['derived_from']} in family {p_fam} "
                    f"({p_split}), but the record is {family} ({rec['split']}), so the "
                    "derived example crosses families or splits"
                )
            for text in _claim_texts(rec):
                text_splits[normalise(text)].add(rec["split"])
            problems += _check_evidence(where, rec)
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


def _parse_jsonl(text: str | None) -> list[Record]:
    return [json.loads(line) for line in (text or "").splitlines() if line.strip()]


def load_data(read: freeze.Reader) -> dict[str, list[Record]]:
    return {n: _parse_jsonl(read(f"pilot/generated/{n}.jsonl")) for n in DATASETS}


# ------------------------------------------------------------------------ main check
def run_checks(
    overlay: Mapping[str, str] | None = None,
    base: str | None = None,
    git_history: bool = False,
) -> tuple[list[str], str]:
    """``overlay`` replaces files under the M7 fixture directory (tests only)."""

    read = freeze.disk_reader(overlay)
    manifest, snap_problems = freeze.check_snapshot(read)
    problems = [f"freeze: {p}" for p in snap_problems]
    if manifest is None:
        return problems, "no active manifest"
    snaps = json.loads(read(freeze.SNAPSHOT_FILE) or "{}")
    active = snaps["manifests"][snaps["active"]]
    if base:
        problems += [f"freeze (base {base}): {p}" for p in freeze.check_against_base(base)]
    if git_history:
        problems += [
            f"freeze order: {p}"
            for p in freeze.check_freeze_order(active["lock"], "pilot/datasets.json")
        ]
    problems += [f"corpus-v3: {p}" for p in corpus_v3.check_corpus()]
    problems += [f"corpus-v3: {p}" for p in corpus_assignment_problems(manifest)]
    report = overlap.overlap_report(manifest)
    problems += [f"overlap: {p}" for p in overlap.overlap_problems(report)]
    legacy, legacy_problems = legacy_assignments(manifest)
    problems += [f"legacy: {p}" for p in legacy_problems]
    seeds = load_seeds()
    if seeds.get("manifest") != manifest.name:
        problems.append(f"seeds: written for {seeds.get('manifest')}, active is {manifest.name}")
    for rel, text in sorted(render(manifest).items()):
        if read(f"pilot/{rel}") != text:
            problems.append(
                f"determinism: pilot/{rel} differs from a fresh build "
                "(run `python -m app.evaluation build-pilot` and review the diff)"
            )
    index = json.loads(read("pilot/datasets.json") or "{}")
    if index.get("manifest_sha256") != manifest.sha256:
        problems.append("datasets.json was not generated from the active frozen manifest")
    data = load_data(read)
    problems += [f"records: {p}" for p in check_records(manifest, data, seeds, legacy)]
    label_status = lab.status()
    problems += [f"labels: {p}" for p in label_status["problems"]]
    return problems, format_report(counts_report(manifest, data, legacy, label_status))


def counts_report(
    manifest: Manifest | None = None,
    data: Mapping[str, list[Record]] | None = None,
    legacy: LegacyIndex | None = None,
    label_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if manifest is None:
        manifest, _ = freeze.check_snapshot(freeze.disk_reader())
        if manifest is None:
            raise RuntimeError("no active manifest in frozen-snapshots.json")
    if data is None:
        data = load_data(freeze.disk_reader())
    if legacy is None:
        legacy, _ = legacy_assignments(manifest)
    label_status = label_status if label_status is not None else lab.status()
    fam_counts: dict[str, int] = defaultdict(int)
    for fam in manifest.family_split.values():
        fam_counts[fam] += 1
    doc_counts: dict[str, int] = defaultdict(int)
    for ref, fam in manifest.member_family.items():
        if not ref.startswith("sql:"):
            doc_counts[manifest.family_split[fam]] += 1
    return {
        "manifest": {
            "name": manifest.name,
            "sha256": manifest.sha256,
            "families_by_split": dict(sorted(fam_counts.items())),
            "source_files_by_split": dict(sorted(doc_counts.items())),
            "reserved_fresh_families": dict(sorted(manifest.reserved.items())),
            "sealed_families": sorted(manifest.sealed()),
        },
        "legacy_items_checked": len(legacy),
        "datasets": {
            name: {
                "count": len(data[name]),
                "by_split": {
                    s: sum(1 for r in data[name] if r["split"] == s) for s in manifest.splits
                },
                "intended_split": INTENDED_SPLIT[name],
                "in_intended_split": sum(
                    1 for r in data[name] if r["split"] == INTENDED_SPLIT[name]
                ),
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
        f"Active split manifest {m['name']} sha256={m['sha256'][:16]}…",
        f"  families by split: {m['families_by_split']}",
        f"  source files by split: {m['source_files_by_split']}",
        f"  reserved fresh families (no members yet): {m['reserved_fresh_families']}",
        f"Pre-M7 fixture items mapped to families: {report['legacy_items_checked']}",
        "",
        "PILOT datasets (not the release evaluation; targets are roadmap plans):",
        f"  {'dataset':<22}{'count':>6}  {'train':>5} {'dev':>5} {'heldout':>7}"
        f"  {'in intended split':>18}  target",
    ]
    for name, d in report["datasets"].items():
        s = d["by_split"]
        intended = f"{d['in_intended_split']} ({d['intended_split']})"
        lines.append(
            f"  {name:<22}{d['count']:>6}  {s['train']:>5} {s['dev']:>5} "
            f"{s['heldout']:>7}  {intended:>18}  {d['roadmap_target']}"
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
