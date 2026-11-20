"""Deterministic checks on narrative sentences before any NLI scoring.

Every number in a sentence must equal (as an exact decimal) a cell of the evidence row
that the sentence cites, and every cited cell's figure must appear in the sentence, so
each sentence states at least one exact verified figure. Period labels and the period's
own dates are the only other numbers allowed. Percentages, rounded figures ("80.9
million") and unknown numbers fail. Measure words must not contradict the cited column
(net vs gross). A failing sentence is never shown; its failure codes drive a repair.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.reporting.contracts import OUTPUT_COLUMNS, SalesReportIntent

_MONTHS = "|".join(name for name in calendar.month_name if name)
_PERIOD_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\bQ[1-4]\s*\d{4}\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{4}}\b", re.I),
)
_NUMBER = re.compile(
    r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w.,])-?\d+(?:\.\d+)?(?![\w,])"
)
_SCALE = re.compile(r"\b(thousand|million|billion|bn|mn|k)\b", re.I)
_PERCENT = re.compile(r"\d\s*(%|percent\b)", re.I)
_NET = re.compile(r"\bnet\b|tax[- ]exclusive|excluding tax|before tax", re.I)
_GROSS = re.compile(r"\bgross\b|tax[- ]inclusive|including tax|after tax", re.I)


@dataclass
class SentenceCheck:
    index: int
    failures: list[str] = field(default_factory=list)
    matched: dict[str, str] = field(default_factory=dict)  # number text -> column


def _period_years(intent: SalesReportIntent) -> set[str]:
    return {str(intent.period.start.year), str(intent.period.end.year)}


def _strip_period_text(text: str, intent: SalesReportIntent) -> str:
    for pattern in _PERIOD_PATTERNS:
        text = pattern.sub(" ", text)
    for year in _period_years(intent):
        text = re.sub(rf"\b{year}\b", " ", text)
    return text


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def check_sentence(
    index: int, sentence: dict[str, Any], row: dict[str, Any], intent: SalesReportIntent
) -> SentenceCheck:
    result = SentenceCheck(index=index)
    text: str = sentence["text"]
    cites: list[str] = sentence["cites"]
    unknown = [c for c in cites if c not in OUTPUT_COLUMNS]
    if unknown:
        result.failures.append("unknown_citation")
    if not cites:
        result.failures.append("uncited_sentence")
    if _PERCENT.search(text):
        result.failures.append("percentage_not_in_evidence")
    if _SCALE.search(text):
        result.failures.append("rounded_or_scaled_figure")
    cited = {c: row[c] for c in cites if c in OUTPUT_COLUMNS}
    for match in _NUMBER.finditer(_strip_period_text(text, intent)):
        token = match.group(0)
        value = _decimal(token)
        column = next(
            (c for c, cell in cited.items() if value is not None and Decimal(str(cell)) == value),
            None,
        )
        if column is None:
            evidence_hit = any(
                value is not None and Decimal(str(row[c])) == value for c in OUTPUT_COLUMNS
            )
            result.failures.append("figure_not_cited" if evidence_hit else "figure_not_in_evidence")
            continue
        if column != "order_count" and "." not in token:
            # Money must be written with its exact two decimals.
            result.failures.append("figure_not_exact")
        result.matched[token] = column
    columns = set(result.matched.values())
    # A citation must be backed by that exact figure in the sentence; figure-free
    # qualitative claims ("sales boomed") cannot borrow a citation.
    if any(c not in columns for c in cited):
        result.failures.append("cited_figure_missing")
    if "gross_sales" in columns and _NET.search(text) and not _GROSS.search(text):
        result.failures.append("gross_described_as_net")
    if "net_sales" in columns and _GROSS.search(text) and not _NET.search(text):
        result.failures.append("net_described_as_gross")
    return result


def check_sentences(
    sentences: list[dict[str, Any]], row: dict[str, Any], intent: SalesReportIntent
) -> list[SentenceCheck]:
    return [check_sentence(i, s, row, intent) for i, s in enumerate(sentences)]
