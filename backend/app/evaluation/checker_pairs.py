"""M7 checker training pairs, batch 001: silver (claim, evidence) pairs from the train split.

Input comes only from ``app.evaluation.loader.load("training")``, so dev and held-out
documents are never opened. Every pair:

* has an **atomic claim**: one sentence stating one fact of one document;
* cites an **exact evidence span**: one sentence of the document's canonical text, with
  its character offsets, plus the document title (``premise = title + span``, the
  ``title + chunk`` shape the application checks);
* is traceable to its **source family**, its source document's frozen-lock fingerprint,
  and the fact it states;
* is **silver**: labelled by construction, not reviewed by a person.

For each supported claim, the unsupported variants keep the same parent document, family
and split:
  swap            the value replaced by the same fact from another document of the family
  shift           a nearby number or date (minutes, days, money, years)
  distractor      a neighbouring value from the same document (estimate vs final cost,
                  raised vs closed)
  polarity        the negated statement (yes/no facts)
  wrong_evidence  the original claim against the same fact's sentence in another document
                  of the same family (that document's own value differs)

Determinism: values come from the loaded facts, sorted. Random choices use
``random.Random`` seeded from SHA-256(BATCH_SEED:pair id). Output is sorted, with sorted
keys. Exact duplicate pairs are removed and counted. No threshold, model or training run
is involved.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evaluation import loader, token_lengths

BATCH = "batch-001"
BATCH_SEED = 20261127
GENERATOR_VERSION = "1.0.0"
OUT_DIR = loader.M7_DIR / "checker" / BATCH
PAIRS_FILE = "pairs.jsonl"
INDEX_FILE = "index.json"
LENGTHS_FILE = "token-lengths.json"
ROADMAP_TARGET = (3000, 5000)
MAX_CLAIM_WORDS = 30

Facts = dict[str, Any]
Pair = dict[str, Any]


def fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B')} {d.year}"


@dataclass(frozen=True)
class Spec:
    """One atomic claim type: which fact, where its evidence is, how it is phrased."""

    name: str
    fact: str
    kind: str  # "int", "money", "date", "text", "bool"
    needle: Callable[[Facts, Any], str]  # a string inside the evidence sentence
    claim: Callable[[Facts, Any], str]
    distractor: str | None = None  # another fact of the same document, same kind
    # "upper": the claim states a bound ("within N hours"). A looser value would still be
    # entailed, so unsupported variants may only make it stricter (smaller).
    bound: str | None = None


def _ref(f: Facts) -> str:
    return str(f["reference"])


SPECS: dict[str, tuple[Spec, ...]] = {
    "fresh-train-regenerated": (
        Spec("asset", "asset", "text", lambda f, v: f"The {v} at the",
             lambda f, v: f"Work order {_ref(f)} is for the {v}."),
        Spec("site", "site", "text", lambda f, v: f"at the {v} site",
             lambda f, v: f"Work order {_ref(f)} is at the {v} site."),
        Spec("priority", "priority", "text", lambda f, v: f"Priority {v}.",
             lambda f, v: f"Work order {_ref(f)} has priority {v}."),
        Spec("response", "response_hours", "int", lambda f, v: f"respond within {v} hours",
             lambda f, v: f"Work order {_ref(f)} requires a response within {v} hours.",
             bound="upper"),
        Spec("vendor", "vendor", "text", lambda f, v: f"{v} must respond",
             lambda f, v: f"{v} must respond to work order {_ref(f)}."),
        Spec("labour", "labour_hours", "int", lambda f, v: f"allows {v} labour hours",
             lambda f, v: f"Work order {_ref(f)} allows {v} labour hours."),
        Spec("technicians", "technicians", "int",
             lambda f, v: f"labour hours for {v} technician",
             lambda f, v: f"Work order {_ref(f)} plans for {v} technician{'s' if v != 1 else ''}."),
        Spec("estimate", "estimate_egp", "money", lambda f, v: f"estimate of EGP {v:,}",
             lambda f, v: f"Work order {_ref(f)} has an estimate of EGP {v:,}.",
             distractor="actual_egp"),
        Spec("final-cost", "actual_egp", "money", lambda f, v: f"final cost of EGP {v:,}",
             lambda f, v: f"Work order {_ref(f)} closed at a final cost of EGP {v:,}.",
             distractor="estimate_egp"),
        Spec("raised", "raised", "date", lambda f, v: f"Raised {fmt_date(v)}.",
             lambda f, v: f"Work order {_ref(f)} was raised on {fmt_date(v)}.",
             distractor="closed"),
        Spec("closed", "closed", "date", lambda f, v: f"Closed on {fmt_date(v)}",
             lambda f, v: f"Work order {_ref(f)} was closed on {fmt_date(v)}.",
             distractor="raised"),
    ),
    "fresh-train-supplier-due-diligence": (
        Spec("category", "category", "text", lambda f, v: f"is a {v} supplier",
             lambda f, v: f"Review {_ref(f)} describes {f['supplier']} as a {v} supplier."),
        Spec("city", "city", "text", lambda f, v: f"based in {v},",
             lambda f, v: f"Review {_ref(f)} records {f['supplier']} as based in {v}."),
        Spec("since", "supplier_since", "year", lambda f, v: f"used since {v}.",
             lambda f, v: f"According to review {_ref(f)}, NileTech has used "
             f"{f['supplier']} since {v}."),
        Spec("tier", "risk_tier", "text", lambda f, v: f"Risk tier: {v}.",
             lambda f, v: f"Review {_ref(f)} rates {f['supplier']} as {v} risk."),
        Spec("cover", "indemnity_cover_usd", "money", lambda f, v: f"cover: USD {v:,}.",
             lambda f, v: f"Review {_ref(f)} records indemnity cover of USD {v:,} for "
             f"{f['supplier']}."),
        Spec("personal-data", "handles_personal_data", "bool",
             lambda f, v: "processes staff names" if v else "receives no personal data",
             lambda f, v: (
                 f"Review {_ref(f)} states that {f['supplier']} processes staff personal data "
                 "for NileTech." if v else
                 f"Review {_ref(f)} states that {f['supplier']} receives no personal data "
                 "from NileTech."
             )),
        Spec("decision", "decision", "text", lambda f, v: f"The supplier is {v}.",
             lambda f, v: f"Review {_ref(f)} records {f['supplier']} as {v}."),
        Spec("next-review", "next_review_months", "int", lambda f, v: f"due in {v} months",
             lambda f, v: f"After review {_ref(f)}, the next review of {f['supplier']} is due "
             f"in {v} months."),
        Spec("reviewed", "reviewed", "date", lambda f, v: f"Reviewed {fmt_date(v)}",
             lambda f, v: f"Review {_ref(f)} was carried out on {fmt_date(v)}."),
    ),
    "fresh-train-incident-review": (
        Spec("service", "service", "text", lambda f, v: f"{_ref(f)}: {v}",
             lambda f, v: f"Incident {_ref(f)} affected the {v}."),
        Spec("severity", "severity", "int", lambda f, v: f"Severity {v}.",
             lambda f, v: f"Incident {_ref(f)} was rated severity {v}."),
        Spec("duration", "duration_minutes", "int", lambda f, v: f"lasted {v} minutes",
             lambda f, v: f"Incident {_ref(f)} lasted {v} minutes."),
        Spec("began", "started", "date", lambda f, v: f"Began {fmt_date(v)}",
             lambda f, v: f"Incident {_ref(f)} began on {fmt_date(v)}.",
             distractor="action_due"),
        Spec("accounts", "affected_accounts", "int", lambda f, v: f"{v} client accounts",
             lambda f, v: f"Incident {_ref(f)} affected {v} client accounts."),
        Spec("cause", "cause", "text", lambda f, v: f"caused by {v}",
             lambda f, v: f"Incident {_ref(f)} was caused by {v}."),
        Spec("owner", "action_owner", "text", lambda f, v: f"The {v} team owns",
             lambda f, v: f"The {v} team owns the corrective action for incident {_ref(f)}."),
        Spec("action-due", "action_due", "date", lambda f, v: f"due {fmt_date(v)}",
             lambda f, v: f"The corrective action for incident {_ref(f)} is due on "
             f"{fmt_date(v)}.",
             distractor="started"),
    ),
}  # fmt: skip

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def sentences(text: str) -> list[tuple[int, str]]:
    """(offset, sentence) for every sentence of every paragraph; the title is one."""

    out: list[tuple[int, str]] = []
    pos = 0
    for para in text.split("\n\n"):
        start = text.index(para, pos)
        cursor = 0
        for sent in _SENTENCE.split(para):
            at = para.index(sent, cursor)
            out.append((start + at, sent))
            cursor = at + len(sent)
        pos = start + len(para)
    return out


def evidence_for(text: str, needle: str) -> tuple[int, str]:
    hits = [(o, s) for o, s in sentences(text) if needle in s]
    if len(hits) != 1:
        raise ValueError(f"needle {needle!r} found in {len(hits)} sentences")
    return hits[0]


def _rng(key: str) -> random.Random:
    digest = hashlib.sha256(f"{BATCH_SEED}:{key}".encode()).hexdigest()
    # Reproducible synthetic data, not security: a seeded PRNG is the point.
    return random.Random(int(digest[:16], 16))  # noqa: S311


def _shift(spec: Spec, value: Any, rng: random.Random) -> Any:
    if spec.kind == "int":
        step = rng.choice((1, 2, 3)) if value < 10 else rng.choice((5, 10, 15, 20))
        return value + step if value - step < 1 or rng.random() < 0.5 else value - step
    if spec.kind == "money":
        step = (
            rng.choice((250, 500, 1_000, 2_500))
            if value < 100_000
            else rng.choice((50_000, 100_000, 250_000))
        )
        return value + step if rng.random() < 0.5 or value - step <= 0 else value - step
    if spec.kind == "year":
        return value + rng.choice((-3, -2, -1, 1, 2)) if value < 2025 else value - 2
    if spec.kind == "date":
        days = rng.choice((-14, -7, -3, 3, 7, 14))
        return (dt.date.fromisoformat(value[:10]) + dt.timedelta(days=days)).isoformat()
    return None


def _base_pair(doc: loader.LoadedDocument, spec: Spec) -> tuple[str, int, str, str]:
    """(title, offset, span, sha) of the evidence for spec's fact in doc."""

    if doc.facts is None:
        raise ValueError(f"{doc.ref} has no facts sidecar")
    title = doc.text.split("\n", 1)[0]
    offset, span = evidence_for(doc.text, spec.needle(doc.facts, doc.facts[spec.fact]))
    return title, offset, span, hashlib.sha256(doc.text.encode()).hexdigest()


def build_pairs(result: loader.LoadResult | None = None) -> tuple[list[Pair], dict[str, Any]]:
    """Generate the batch from the training loader. Returns (pairs, build statistics)."""

    result = result or loader.load(loader.Purpose.TRAINING)
    if result.purpose is not loader.Purpose.TRAINING:
        raise ValueError("checker training pairs must come from the training loader")
    docs = sorted(result.documents, key=lambda d: d.ref)
    for doc in docs:
        if doc.split != "train" or doc.facts is None:
            raise ValueError(f"{doc.ref} is not a train document with facts")
    by_family: dict[str, list[loader.LoadedDocument]] = defaultdict(list)
    for doc in docs:
        by_family[doc.family].append(doc)
    unknown = set(by_family) - set(SPECS)
    if unknown:
        raise ValueError(f"no claim specs for train families {sorted(unknown)}")
    raw: list[Pair] = []
    for family, fam_docs in sorted(by_family.items()):
        for spec in SPECS[family]:
            pool = sorted({json.dumps(d.facts[spec.fact]) for d in fam_docs if d.facts})
            for doc in fam_docs:
                raw += _pairs_for(doc, spec, [json.loads(v) for v in pool], fam_docs)
    seen: dict[tuple[str, str, str], Pair] = {}
    duplicates = conflicts = 0
    for pair in raw:
        key = (pair["claim"], pair["evidence_title"], pair["evidence"])
        if key in seen:
            duplicates += 1
            conflicts += seen[key]["label"] != pair["label"]
            continue
        seen[key] = pair
    pairs = sorted(seen.values(), key=lambda p: p["id"])
    return pairs, {"generated": len(raw), "duplicates_removed": duplicates,
                   "label_conflicts": conflicts}  # fmt: skip


def _pairs_for(
    doc: loader.LoadedDocument,
    spec: Spec,
    pool: list[Any],
    fam_docs: list[loader.LoadedDocument],
) -> list[Pair]:
    facts = doc.facts
    if facts is None:
        raise ValueError(f"{doc.ref} has no facts sidecar")
    value = facts[spec.fact]
    title, offset, span, text_sha = _base_pair(doc, spec)
    key = doc.ref.partition(":")[2]
    parent_id = f"{BATCH}:{key}:{spec.name}:supported"
    rng = _rng(parent_id)

    def pair(kind: str, claim_value: Any, label: str, evidence_doc: loader.LoadedDocument,
             ev: tuple[str, int, str, str]) -> Pair:  # fmt: skip
        pid = parent_id if kind == "original" else f"{BATCH}:{key}:{spec.name}:{kind}"
        return {
            "id": pid,
            "batch": BATCH,
            "family": doc.family,
            "split": doc.split,
            "source": doc.ref,
            "fact": spec.fact,
            "claim_type": spec.name,
            "claim": spec.claim(facts, claim_value),
            "evidence_source": evidence_doc.ref,
            "evidence_title": ev[0],
            "evidence": ev[2],
            "evidence_start": ev[1],
            "evidence_end": ev[1] + len(ev[2]),
            "evidence_text_sha256": ev[3],
            # The exact checker input: premise = title + evidence (the title alone when the
            # evidence is the title line), hypothesis = claim.
            "premise": ev[0] if ev[2] == ev[0] else f"{ev[0]}\n\n{ev[2]}",
            "variant": kind,
            "parent": None if kind == "original" else parent_id,
            "label": label,
            "label_quality": "silver",
            "label_source": "rule: generated from the document's facts sidecar; not human-reviewed",
        }

    ev = (title, offset, span, text_sha)
    out = [pair("original", value, "supported", doc, ev)]
    others = [v for v in pool if v != value and (spec.bound != "upper" or v < value)]
    if spec.kind == "bool":
        out.append(pair("polarity", not value, "unsupported", doc, ev))
    elif others:
        out.append(pair("swap", rng.choice(others), "unsupported", doc, ev))
    shifted = _shift(spec, value, rng)
    if spec.bound == "upper" and shifted is not None and shifted >= value:
        shifted = value - 1 if value > 1 else None
    if shifted is not None and shifted != value:
        out.append(pair("shift", shifted, "unsupported", doc, ev))
    if spec.distractor and facts[spec.distractor] != value:
        out.append(pair("distractor", facts[spec.distractor], "unsupported", doc, ev))
    candidates = [
        d for d in fam_docs if d.ref != doc.ref and d.facts and d.facts[spec.fact] != value
    ]
    if candidates:
        other = rng.choice(candidates)
        out.append(pair("wrong_evidence", value, "unsupported", other, _base_pair(other, spec)))
    return out


# ------------------------------------------------------------------- validation
def validate(pairs: list[Pair], result: loader.LoadResult) -> list[str]:
    """Split, provenance, evidence and atomicity rules; result must be the training load."""

    problems: list[str] = []
    if result.purpose is not loader.Purpose.TRAINING:
        problems.append("validation must use the training loader")
    texts = {d.ref: d for d in result.documents}
    train_families = {d.family for d in result.documents}
    by_id = {p["id"]: p for p in pairs}
    if len(by_id) != len(pairs):
        problems.append("duplicate pair ids")
    seen: dict[tuple[str, str, str], str] = {}
    for p in pairs:
        where = p["id"]
        for ref in (p["source"], p["evidence_source"]):
            if ref not in texts:
                problems.append(f"{where}: {ref} is not a train document (dev/held-out/unknown)")
        if p["split"] != "train" or p["family"] not in train_families:
            problems.append(f"{where}: split {p['split']} / family {p['family']} is not train")
        if p["evidence_source"] in texts and texts[p["evidence_source"]].family != p["family"]:
            problems.append(f"{where}: evidence comes from another family")
        if p["label_quality"] != "silver" or p["label"] not in ("supported", "unsupported"):
            problems.append(f"{where}: labels must be silver supported/unsupported")
        if (p["variant"] == "original") != (p["label"] == "supported"):
            problems.append(f"{where}: only the original claim may be supported")
        if p["parent"] is not None:
            parent = by_id.get(p["parent"])
            if parent is None:
                problems.append(f"{where}: unknown parent {p['parent']}")
            elif (parent["family"], parent["split"], parent["source"]) != (
                p["family"],
                p["split"],
                p["source"],
            ):
                problems.append(f"{where}: variant left its parent's family, split or source")
        doc = texts.get(p["evidence_source"])
        if doc is not None:
            span = doc.text[p["evidence_start"] : p["evidence_end"]]
            if span != p["evidence"]:
                problems.append(f"{where}: evidence is not the exact span at its offsets")
            if doc.text.split("\n", 1)[0] != p["evidence_title"]:
                problems.append(f"{where}: evidence title is not the source document's title")
        expected = (
            p["evidence_title"]
            if p["evidence"] == p["evidence_title"]
            else (f"{p['evidence_title']}\n\n{p['evidence']}")
        )
        if p["premise"] != expected:
            problems.append(f"{where}: premise is not title + evidence")
        claim = p["claim"].strip()
        if ";" in claim or re.search(r"[.!?]\s+[A-Z]", claim) or len(claim.split()) > 30:
            problems.append(f"{where}: claim is not atomic")
        key = (p["claim"], p["evidence_title"], p["evidence"])
        if key in seen:
            problems.append(f"{where}: duplicate of {seen[key]}")
        seen[key] = where
    return problems


# --------------------------------------------------------------------- files
def _jsonl(pairs: list[Pair]) -> str:
    return "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in pairs)


def render(result: loader.LoadResult | None = None) -> dict[str, str]:
    result = result or loader.load(loader.Purpose.TRAINING)
    pairs, stats = build_pairs(result)
    body = _jsonl(pairs)
    by_family: dict[str, Counter[str]] = defaultdict(Counter)
    for p in pairs:
        by_family[p["family"]][p["label"]] += 1
    labels = Counter(p["label"] for p in pairs)
    index = {
        "batch": BATCH,
        "generator": "app/evaluation/checker_pairs.py",
        "generator_version": GENERATOR_VERSION,
        "batch_seed": BATCH_SEED,
        "manifest": result.manifest,
        "loader_purpose": result.purpose.value,
        "source_documents": len(result.documents),
        "label_quality": "silver",
        "pairs_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "pairs": len(pairs),
        "by_label": dict(sorted(labels.items())),
        "by_family": {f: dict(sorted(c.items())) for f, c in sorted(by_family.items())},
        "by_variant": dict(sorted(Counter(p["variant"] for p in pairs).items())),
        "generated_before_dedup": stats["generated"],
        "duplicates_removed": stats["duplicates_removed"],
        "label_conflicts": stats["label_conflicts"],
        "roadmap_target": f"{ROADMAP_TARGET[0]:,}-{ROADMAP_TARGET[1]:,} checker training pairs",
        "gap_to_target_lower_bound": max(0, ROADMAP_TARGET[0] - len(pairs)),
    }
    return {PAIRS_FILE: body, INDEX_FILE: json.dumps(index, indent=2, sort_keys=True) + "\n"}


def write(out_dir: Path = OUT_DIR) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = render()
    for name, text in files.items():
        (out_dir / name).write_text(text, "utf-8")
    index: dict[str, Any] = json.loads(files[INDEX_FILE])
    return index


def check(out_dir: Path = OUT_DIR) -> list[str]:
    """Committed batch == fresh build, and every rule in ``validate`` holds."""

    result = loader.load(loader.Purpose.TRAINING)
    problems: list[str] = []
    for name, text in render(result).items():
        path = out_dir / name
        if not path.is_file() or path.read_text("utf-8") != text:
            problems.append(f"checker/{BATCH}/{name} differs from a fresh build")
    pairs, stats = build_pairs(result)
    if stats["label_conflicts"]:
        problems.append(f"{stats['label_conflicts']} identical pairs carry different labels")
    problems += validate(pairs, result)
    return problems


def token_report_problems(out_dir: Path = OUT_DIR) -> list[str]:
    """The committed token report must describe exactly the committed pairs, measured with
    the pinned checker tokenizer, with no pair over the gateway's limit."""

    path = out_dir / LENGTHS_FILE
    if not path.is_file():
        return [f"checker/{BATCH}/{LENGTHS_FILE} is missing"]
    report = json.loads(path.read_text("utf-8"))
    pairs_bytes = (out_dir / PAIRS_FILE).read_bytes()
    ids = {json.loads(line)["id"] for line in pairs_bytes.decode("utf-8").splitlines() if line}
    problems: list[str] = []
    if report["pairs_sha256"] != hashlib.sha256(pairs_bytes).hexdigest():
        problems.append("token report was measured on different pairs; recount")
    if set(report["lengths"]) != ids:
        problems.append("token report does not cover exactly the committed pair ids")
    tok = report["tokenizer"]
    settings = Settings.model_construct()
    pinned = (settings.model_checker_name, settings.model_checker_revision)
    if (tok["repository"], tok["revision"]) != pinned:
        problems.append(f"token report used {tok['repository']}@{tok['revision']}, not {pinned}")
    if (tok["repository"], tok["revision"]) != (token_lengths.REPO, token_lengths.REVISION):
        problems.append("token report does not match the pins in token_lengths.py")
    if tok["files_sha256"] != token_lengths.FILES:
        problems.append("token report tokenizer files differ from the pinned SHA-256s")
    if report["limit"] != token_lengths.LIMIT or report["overall"]["over_limit"]:
        problems.append(f"{report['overall']['over_limit']} pairs exceed {report['limit']} tokens")
    return problems
