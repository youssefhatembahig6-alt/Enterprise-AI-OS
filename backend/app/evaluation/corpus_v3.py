"""Deterministic generator for the M7 v3 fresh corpus (train and held-out families).

Why a new generator. The original NileTech generator (version 0.1.6, old repository
``enterprise-ai-oshh`` at commit bf4a247a5c3db8bcedc37e0ed47dad6c635d9349,
``scripts/seed``) was located, but it cannot produce fresh families:
  * its policy, security and public-services templates are where the dev corpus
    came from. The dev documents reuse its sentences verbatim (e.g. "Replace manual
    back-office steps with governed workflows.") and restate its stated values
    (14-character passwords, 5 carry-over days, 21 leave days in Egypt);
  * it depends on the old ``eaios_core`` package and on the removed second tenant.
See docs/evaluation/m7-v3-corpus.md.

What this generator guarantees:
  * five families, one template each, with disjoint name pools and fact ranges.
    Every family's template and facts are new: no dev document, the original
    generator's templates or another family uses them (checked by
    ``app.evaluation.overlap``);
  * byte-identical output for the same SEED and GENERATOR_VERSION. One
    ``random.Random`` per family is seeded from SHA-256(seed:family). There are no
    clocks, UUIDs or unordered iteration; dates are offsets from REFERENCE_DATE;
  * a facts sidecar per document: the ground truth a later, separately reviewed
    step will derive claims from. No claim or question is written here.

Run ``python -m app.evaluation build-corpus-v3``. The M7 check rebuilds in memory and
compares the result with the committed files.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.evaluation.sources import M7

GENERATOR_NAME = "eaios-m7-fresh-corpus"
GENERATOR_VERSION = "3.0.0"
SEED = 20261126
REFERENCE_DATE = dt.date(2026, 6, 30)
CORPUS_DIR = M7 / "corpus-v3"
GENERATOR_SOURCE = Path(__file__)

Facts = dict[str, Any]


@dataclass(frozen=True)
class FamilySpec:
    family: str
    split: str
    template: str
    prefix: str
    count: int
    render: Callable[[random.Random, int], tuple[str, str, Facts]]  # (title, text, facts)


def _rng(family: str) -> random.Random:
    digest = hashlib.sha256(f"{SEED}:{family}".encode()).hexdigest()
    # Reproducible synthetic data, not security: a seeded PRNG is the point.
    return random.Random(int(digest[:16], 16))  # noqa: S311


def _date(rng: random.Random, lo: int, hi: int) -> dt.date:
    return REFERENCE_DATE - dt.timedelta(days=rng.randint(lo, hi))


def _fmt(d: dt.date) -> str:
    return f"{d.day} {d.strftime('%B')} {d.year}"


# ---------------------------------------------------------------- train: work orders
_WO_SITES = ("Maadi Annex", "Smart Village Block C", "New Capital Tower 4", "Sidi Gaber Hub")
_WO_ASSETS = {
    "chiller unit": "is short-cycling and the third-floor lab is above 27 °C",
    "UPS battery bank": "failed its monthly load test after 6 minutes",
    "access-control reader": "rejects valid badges at the loading-bay door",
    "fire-suppression panel": "shows a persistent zone-two fault",
    "standby generator": "did not start during the weekly no-load run",
    "passenger lift": "stops between the second and third floors",
}
_WO_VENDORS = ("Karnak Facility Services", "Sphinx Electromech", "Pharos Building Care")
_WO_PRIORITY = {"P1": 4, "P2": 24, "P3": 72}


def _work_order(rng: random.Random, n: int) -> tuple[str, str, Facts]:
    asset = rng.choice(sorted(_WO_ASSETS))
    site = rng.choice(_WO_SITES)
    priority = rng.choice(sorted(_WO_PRIORITY))
    techs = rng.randint(1, 4)
    hours = rng.randint(2, 16)
    estimate = rng.randrange(4_000, 90_000, 250)
    actual = estimate + rng.randrange(-1_500, 6_000, 250)
    raised = _date(rng, 20, 170)
    closed = raised + dt.timedelta(days=rng.randint(1, 12))
    vendor = rng.choice(_WO_VENDORS)
    ref = f"WO-2026-{n:04d}"
    facts = {
        "reference": ref,
        "site": site,
        "asset": asset,
        "priority": priority,
        "response_hours": _WO_PRIORITY[priority],
        "vendor": vendor,
        "technicians": techs,
        "labour_hours": hours,
        "estimate_egp": estimate,
        "actual_egp": actual,
        "raised": raised.isoformat(),
        "closed": closed.isoformat(),
    }
    title = f"Work order {ref}: {asset} at {site}"
    text = (
        f"# {title}\n\n"
        f"Synthetic M7 fixture. Raised {_fmt(raised)}. Priority {priority}.\n\n"
        f"## Request\n\nThe {asset} at the {site} site {_WO_ASSETS[asset]}.\n\n"
        f"## Plan\n\n{vendor} must respond within {_WO_PRIORITY[priority]} hours for a "
        f"{priority} job. The plan allows {hours} labour hours for {techs} "
        f"technician{'s' if techs > 1 else ''}, with an estimate of EGP {estimate:,}.\n\n"
        f"## Closure\n\nClosed on {_fmt(closed)} at a final cost of EGP {actual:,}.\n"
    )
    return title, text, facts


# ------------------------------------------------------- train: supplier due diligence
_DD_SUPPLIERS = (
    ("Qena Print Works", "printing", "Qena", "Egypt"),
    ("Tanta Cable Assemblies", "network cabling", "Tanta", "Egypt"),
    ("Sharjah Badge Systems", "identity badge", "Sharjah", "United Arab Emirates"),
    ("Amman Translation House", "translation", "Amman", "Jordan"),
    ("Muscat Catering Partners", "office catering", "Muscat", "Oman"),
    ("Ismailia Courier Lines", "courier", "Ismailia", "Egypt"),
)
_DD_TIERS = ("low", "medium", "high")


def _due_diligence(rng: random.Random, n: int) -> tuple[str, str, Facts]:
    name, category, city, country = rng.choice(_DD_SUPPLIERS)
    tier = rng.choice(_DD_TIERS)
    cover = rng.randrange(250_000, 3_000_000, 50_000)
    since = rng.randint(2015, 2025)
    reviewed = _date(rng, 10, 160)
    next_months = {"low": 24, "medium": 12, "high": 6}[tier]
    handles_personal_data = rng.random() < 0.5
    decision = {"low": "approved", "medium": "approved with conditions", "high": "suspended"}[tier]
    ref = f"SDR-{n:03d}"
    facts = {
        "reference": ref,
        "supplier": name,
        "category": category,
        "city": city,
        "country": country,
        "supplier_since": since,
        "risk_tier": tier,
        "indemnity_cover_usd": cover,
        "handles_personal_data": handles_personal_data,
        "decision": decision,
        "next_review_months": next_months,
        "reviewed": reviewed.isoformat(),
    }
    data_line = (
        "The supplier processes staff names and phone numbers on NileTech's behalf."
        if handles_personal_data
        else "The supplier receives no personal data from NileTech."
    )
    title = f"Supplier due-diligence review {ref}: {name}"
    text = (
        f"# {title}\n\n"
        f"Synthetic M7 fixture. Reviewed {_fmt(reviewed)} by Procurement.\n\n"
        f"{name} is a {category} supplier based in {city}, {country}, used since "
        f"{since}.\n\n"
        f"## Findings\n\nRisk tier: {tier}. Professional indemnity cover: USD {cover:,}. "
        f"{data_line}\n\n"
        f"## Decision\n\nThe supplier is {decision}. The next review is due in "
        f"{next_months} months.\n"
    )
    return title, text, facts


# --------------------------------------------------- train: post-incident reviews
_PIR_SERVICES = {
    "invoice export API": "download monthly invoice files",
    "single sign-on gateway": "sign in to the client portal",
    "report scheduler": "receive scheduled dashboard emails",
    "document upload service": "upload supporting documents",
}
_PIR_CAUSES = (
    "an expired TLS certificate on an internal load balancer",
    "a database connection pool sized for the previous release",
    "a configuration flag enabled in production before its migration ran",
    "a full disk on the message broker after log rotation stopped",
)
_PIR_TEAMS = ("Platform", "Identity", "Data Services", "Site Reliability")


def _incident(rng: random.Random, n: int) -> tuple[str, str, Facts]:
    service = rng.choice(sorted(_PIR_SERVICES))
    severity = rng.randint(1, 3)
    minutes = rng.randint(12, 240)
    accounts = rng.randint(3, 140)
    cause = rng.choice(_PIR_CAUSES)
    team = rng.choice(_PIR_TEAMS)
    started = _date(rng, 5, 150)
    hour, minute = rng.randint(0, 23), rng.choice((0, 5, 10, 20, 35, 40, 50))
    due = started + dt.timedelta(days=rng.randint(7, 30))
    ref = f"INC-{4100 + n}"
    facts = {
        "reference": ref,
        "service": service,
        "severity": severity,
        "duration_minutes": minutes,
        "affected_accounts": accounts,
        "cause": cause,
        "action_owner": team,
        "started": f"{started.isoformat()}T{hour:02d}:{minute:02d}",
        "action_due": due.isoformat(),
    }
    title = f"Post-incident review {ref}: {service}"
    text = (
        f"# {title}\n\n"
        f"Synthetic M7 fixture. Severity {severity}. Began {_fmt(started)} at "
        f"{hour:02d}:{minute:02d} and lasted {minutes} minutes.\n\n"
        f"## Impact\n\n{accounts} client accounts could not {_PIR_SERVICES[service]} "
        f"during the outage.\n\n"
        f"## Cause\n\nThe outage was caused by {cause}.\n\n"
        f"## Follow-up\n\nThe {team} team owns the corrective action, due {_fmt(due)}.\n"
    )
    return title, text, facts


# ---------------------------------------------- held-out A: client project status
_PS_CLIENTS = (
    ("Abydos Pharmaceuticals", "pharmaceutical"),
    ("Siwa Mineral Water Co.", "beverage"),
    ("Rosetta Grain Exchange", "commodity trading"),
    ("Fayoum Agritech", "agriculture"),
    ("Dahab Dive Resorts", "hospitality"),
)
_PS_WORKSTREAMS = ("data migration", "user acceptance testing", "integration build", "training")
_PS_STATUS = ("green", "amber", "red")


def _project_status(rng: random.Random, n: int) -> tuple[str, str, Facts]:
    client, sector = rng.choice(_PS_CLIENTS)
    stream = rng.choice(_PS_WORKSTREAMS)
    status = rng.choice(_PS_STATUS)
    budget = rng.randrange(800, 6_000, 40)
    used_pct = rng.randint(18, 97)
    period_end = _date(rng, 7, 120)
    next_date = period_end + dt.timedelta(days=rng.randint(10, 60))
    code = f"PRJ-{client.split()[0][:3].upper()}-{n:02d}"
    facts = {
        "project": code,
        "client": client,
        "sector": sector,
        "workstream": stream,
        "status": status,
        "budget_hours": budget,
        "hours_used_percent": used_pct,
        "period_end": period_end.isoformat(),
        "next_milestone_date": next_date.isoformat(),
    }
    risk = {
        "green": "No risk currently threatens the plan.",
        "amber": f"The {stream} workstream depends on client staff who are only part-time.",
        "red": f"The {stream} workstream is blocked until the client signs off its scope.",
    }[status]
    title = f"Project status {code}: {client}"
    text = (
        f"# {title}\n\n"
        f"Synthetic M7 fixture. Period ending {_fmt(period_end)}. Overall status: "
        f"{status}.\n\n"
        f"## Progress\n\nThis {sector} client's {stream} workstream has used {used_pct}% "
        f"of its {budget:,} budgeted hours.\n\n"
        f"## Risks\n\n{risk}\n\n"
        f"## Next milestone\n\nThe next {stream} milestone is due on {_fmt(next_date)}.\n"
    )
    return title, text, facts


# ---------------------------------------------- held-out B: purchase requisitions
_PR_ITEMS = (
    ("ergonomic chairs", "the new Alexandria floor"),
    ("27-inch monitors", "the analytics team's second screens"),
    ("conference-room cameras", "the four meeting rooms on level 5"),
    ("laptop docking stations", "hot-desk positions"),
    ("noise-cancelling headsets", "the support desk"),
)
_PR_SUPPLIERS = ("Giza Office Supply", "Luxor Tech Trading", "Port Said Distributors")
_PR_APPROVAL_LIMIT = 150_000


def _requisition(rng: random.Random, n: int) -> tuple[str, str, Facts]:
    item, purpose = rng.choice(_PR_ITEMS)
    qty = rng.randint(4, 60)
    quotes = sorted(
        ((s, rng.randrange(900, 9_000, 10), rng.randint(3, 30)) for s in _PR_SUPPLIERS),
        key=lambda q: q[0],
    )
    cheapest = min(quotes, key=lambda q: (q[1], q[0]))
    total = cheapest[1] * qty
    needs_director = total > _PR_APPROVAL_LIMIT
    raised = _date(rng, 5, 140)
    ref = f"PR-26-{n:03d}"
    facts = {
        "reference": ref,
        "item": item,
        "quantity": qty,
        "quotes": [{"supplier": s, "unit_egp": p, "delivery_days": d} for s, p, d in quotes],
        "awarded_to": cheapest[0],
        "total_egp": total,
        "director_approval": needs_director,
        "raised": raised.isoformat(),
    }
    rows = "\n".join(f"| {s} | {p:,} | {d} |" for s, p, d in quotes)
    approval = (
        f"Because the total exceeds EGP {_PR_APPROVAL_LIMIT:,}, an operations director "
        "must approve it."
        if needs_director
        else f"The total is within the EGP {_PR_APPROVAL_LIMIT:,} limit for team leads."
    )
    title = f"Purchase requisition {ref}: {item}"
    text = (
        f"# {title}\n\n"
        f"Synthetic M7 fixture. Raised {_fmt(raised)}.\n\n"
        f"## Need\n\n{qty} {item} for {purpose}.\n\n"
        f"## Quotes\n\n| Supplier | Unit price (EGP) | Delivery days |\n| --- | --- | --- |\n"
        f"{rows}\n\n"
        f"## Recommendation\n\nAward to {cheapest[0]}, the lowest unit price, for EGP "
        f"{total:,} in total. {approval}\n"
    )
    return title, text, facts


FAMILIES: tuple[FamilySpec, ...] = (
    FamilySpec("fresh-train-regenerated", "train", "facility-work-order", "wo", 20, _work_order),
    FamilySpec(
        "fresh-train-supplier-due-diligence",
        "train",
        "supplier-due-diligence",
        "sdr",
        20,
        _due_diligence,
    ),
    FamilySpec(
        "fresh-train-incident-review", "train", "post-incident-review", "pir", 20, _incident
    ),
    FamilySpec(
        "fresh-heldout-regenerated-a",
        "heldout",
        "client-project-status",
        "ps",
        12,
        _project_status,
    ),
    FamilySpec(
        "fresh-heldout-regenerated-b",
        "heldout",
        "purchase-requisition",
        "pr",
        12,
        _requisition,
    ),
)


def generator_sha256() -> str:
    return hashlib.sha256(GENERATOR_SOURCE.read_bytes()).hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def generate() -> dict[str, str]:
    """Relative path (under corpus-v3/) -> exact file text, for every corpus file."""

    files: dict[str, str] = {}
    entries: list[str] = []
    for spec in FAMILIES:
        rng = _rng(spec.family)
        for n in range(1, spec.count + 1):
            title, text, facts = spec.render(rng, n)
            key = f"v3-{spec.prefix}-{n:03d}"
            doc_rel, facts_rel = f"files/{key}.md", f"facts/{key}.json"
            facts_text = json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            files[doc_rel], files[facts_rel] = text, facts_text
            entries.append(
                "\n".join(
                    [
                        "[[documents]]",
                        f"key = {_toml_str(key)}",
                        f"family = {_toml_str(spec.family)}",
                        f"split = {_toml_str(spec.split)}",
                        f"template = {_toml_str(spec.template)}",
                        f"title = {_toml_str(title)}",
                        f"file = {_toml_str(doc_rel)}",
                        f"facts = {_toml_str(facts_rel)}",
                        f"sha256 = {_toml_str(_sha(text))}",
                        f"facts_sha256 = {_toml_str(_sha(facts_text))}",
                    ]
                )
            )
    header = "\n".join(
        [
            "# M7 v3 fresh corpus. GENERATED: do not edit. Rebuild with",
            "# `python -m app.evaluation build-corpus-v3`; the M7 check compares a rebuild.",
            "",
            f"generator = {_toml_str(GENERATOR_NAME)}",
            f"generator_version = {_toml_str(GENERATOR_VERSION)}",
            f"generator_source = {_toml_str('backend/app/evaluation/corpus_v3.py')}",
            f"generator_sha256 = {_toml_str(generator_sha256())}",
            f"seed = {SEED}",
            f"reference_date = {_toml_str(REFERENCE_DATE.isoformat())}",
            'original_generator = { repository = "youssefhatembahig6-alt/enterprise-ai-oshh", '
            'commit = "bf4a247a5c3db8bcedc37e0ed47dad6c635d9349", path = "scripts/seed", '
            'status = "located, not used: its templates and stated values are the source of '
            'the dev corpus" }',
            "",
        ]
    )
    files["corpus.toml"] = header + "\n" + "\n\n".join(entries) + "\n"
    return files


def write_corpus(out_dir: Path = CORPUS_DIR) -> int:
    files = generate()
    for rel, text in files.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    return sum(1 for rel in files if rel.startswith("files/"))


def check_corpus(out_dir: Path = CORPUS_DIR) -> list[str]:
    """The committed corpus must equal a fresh build, file for file."""

    problems: list[str] = []
    expected = generate()
    on_disk = {
        p.relative_to(out_dir).as_posix()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name != "README.md"
    }
    for rel in sorted(set(expected) | on_disk):
        path = out_dir / rel
        if rel not in expected:
            problems.append(f"corpus-v3/{rel} is not produced by the generator")
        elif not path.is_file() or path.read_text("utf-8") != expected[rel]:
            problems.append(f"corpus-v3/{rel} differs from a fresh build")
    return problems
