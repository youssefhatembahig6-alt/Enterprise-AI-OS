"""Rule-based request routing and period extraction.

The specification routes simple requests by rules and uses the model only when rules
are unsure. The report *intent* (period, statuses, date basis, currency) is always
derived here from the question and the tenant profile, never from model output, so a
model cannot widen or shift what is reported.

Outcomes:
* ``sales_report`` with a trusted intent;
* ``clarify`` when no single supported period is named;
* ``unsupported`` when the request asks for a different definition or grain
  (statuses other than the profile's, invoice/payment basis, breakdowns, other
  currencies, forecasts, comparisons);
* ``ask_model`` when a period is present but the topic words are unrecognised.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.reporting.contracts import Period, ReportProfile, SalesReportIntent

RouteKind = Literal["sales_report", "clarify", "unsupported", "ask_model"]

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORDINAL_Q = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
}

_QUARTER_PATTERNS = (
    re.compile(r"\bq([1-4])\s*(?:of\s+)?(?:fy\s*)?(\d{4})\b", re.I),
    re.compile(r"\b(\d{4})\s*[-/ ]?\s*q([1-4])\b", re.I),
    re.compile(
        r"\b(first|1st|second|2nd|third|3rd|fourth|4th)\s+quarter\s+(?:of\s+)?(\d{4})\b", re.I
    ),
)
_MONTH_PATTERN = re.compile(rf"\b({_MONTH_RE})\.?\s+(?:of\s+)?(\d{{4}})\b", re.I)
_YEAR_ONLY = re.compile(r"\b(?:in|for|during)\s+(\d{4})\b", re.I)

_TOPIC = re.compile(r"\b(sales|sold|sell|revenue|turnover|orders?|bookings?)\b", re.I)

# Requests for a different definition or grain than the supported report.
_UNSUPPORTED = (
    (
        "grain",
        re.compile(
            r"\b(by|per|each|breakdown|broken down|split)\s+"
            r"(region|customer|product|sku|month|week|day|rep|salesperson|country|tier)\b",
            re.I,
        ),
    ),
    ("grain", re.compile(r"\b(top|bottom)\s+\d+\b|\branking\b|\baverage\b|\bmedian\b", re.I)),
    (
        "comparison",
        re.compile(
            r"\b(compare|comparison|versus|vs\.?|growth|change from|increase|decrease|trend)\b",
            re.I,
        ),
    ),
    (
        "forecast",
        re.compile(r"\b(forecast|predict|projection|expected|next quarter|next month)\b", re.I),
    ),
    (
        "payment_basis",
        re.compile(
            r"\b(paid|collected|collections|cash|receipts|outstanding|overdue|unpaid|receivable)\b",
            re.I,
        ),
    ),
    ("currency", re.compile(r"\b(eur|euro|egp|aed|gbp|sar|in\s+pounds|convert)\b", re.I)),
)
_NEGATION = re.compile(
    r"\b(excluding|exclude|excludes|excluded|without|except|not|no|ignoring|ignore"
    r"|regardless of|irrespective of)\b[\w\s/,-]{0,40}$",
    re.I,
)
_STATUS_WORDS = re.compile(
    r"\b(pending|cancell?ed|unfulfilled|all statuses|all orders|every order|any status)\b", re.I
)
_INVOICE_DATE = re.compile(r"\b(invoice[ds]?|invoicing|billed|billing)\b", re.I)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    kind: RouteKind
    reason: str
    intent: SalesReportIntent | None = None


def _quarter(q: int, year: int) -> Period:
    start = date(year, 3 * (q - 1) + 1, 1)
    end_month = 3 * q
    end = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return Period(kind="quarter", start=start, end=end, label=f"Q{q} {year}")


def _month(m: int, year: int) -> Period:
    return Period(
        kind="month",
        start=date(year, m, 1),
        end=date(year, m, calendar.monthrange(year, m)[1]),
        label=f"{calendar.month_name[m]} {year}",
    )


def extract_periods(question: str) -> list[Period]:
    found: dict[tuple[date, date], Period] = {}
    for pattern in _QUARTER_PATTERNS:
        for match in pattern.finditer(question):
            a, b = match.group(1), match.group(2)
            if a.isdigit() and len(a) == 4:
                q, year = int(b), int(a)
            elif a.lower() in _ORDINAL_Q:
                q, year = _ORDINAL_Q[a.lower()], int(b)
            else:
                q, year = int(a), int(b)
            period = _quarter(q, year)
            found[(period.start, period.end)] = period
    for match in _MONTH_PATTERN.finditer(question):
        period = _month(_MONTHS[match.group(1).lower()], int(match.group(2)))
        found[(period.start, period.end)] = period
    return list(found.values())


def _unnegated(pattern: re.Pattern[str], question: str) -> bool:
    for match in pattern.finditer(question):
        if not _NEGATION.search(question[: match.start()]):
            return True
    return False


def route(question: str, profile: ReportProfile) -> RouteDecision:
    text = " ".join(question.split())
    for reason, pattern in _UNSUPPORTED:
        if reason == "payment_basis":
            if _unnegated(pattern, text):
                return RouteDecision("unsupported", reason)
        elif pattern.search(text):
            return RouteDecision("unsupported", reason)
    if _unnegated(_STATUS_WORDS, text):
        return RouteDecision("unsupported", "status_definition")
    if _unnegated(_INVOICE_DATE, text):
        return RouteDecision("unsupported", "invoice_basis")

    periods = extract_periods(text)
    if len(periods) != 1:
        if not periods and _YEAR_ONLY.search(text):
            return RouteDecision("clarify", "year_only")
        return RouteDecision("clarify", "no_single_period" if periods else "no_period")
    period = periods[0]
    if period.kind not in profile.periods:
        return RouteDecision("unsupported", "period_kind")
    intent = SalesReportIntent(
        report_key=profile.report_key,
        period=period,
        date_basis=profile.date_basis,
        included_statuses=tuple(profile.included_statuses),
        currency=profile.currency,
    )
    if not _TOPIC.search(text):
        return RouteDecision("ask_model", "topic_unrecognised", intent)
    return RouteDecision("sales_report", "rules", intent)
