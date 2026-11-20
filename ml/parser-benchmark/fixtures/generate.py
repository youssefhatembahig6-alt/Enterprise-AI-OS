"""Generate the M1 parser-benchmark fixtures and their gold references.

Every fixture is SYNTHETIC. Content comes from three labelled sources:

* ``baseline-public``: the PUBLIC ``about.md`` document of the NileTech 0.1.6 archive
  (its SHA-256 is recorded; the archive itself stays outside the repository).
* ``baseline-sales``: the six sales tables already vendored in
  ``backend/fixtures/business/niletech-0.1.6`` (aggregated here with ``Decimal``).
* ``synthetic``: text written for this benchmark. It is not a NileTech publication.

Internal, confidential and restricted baseline documents are never used, because this
repository is public.

The gold references are written from the same data structures that draw the pages, then
checked by eye against rendered page images (see ``fixtures/README.md``). Run twice and the
bytes are identical; ``tests/test_fixtures.py`` checks the committed files against
``manifest.json``.

Usage::

    python fixtures/generate.py            # rewrite files, gold and manifest
    python fixtures/generate.py --check    # regenerate in a temp dir and compare hashes
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import random
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SALES_DIR = REPO / "backend/fixtures/business/niletech-0.1.6"
FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
GENERATOR_VERSION = "m1-parser-fixtures-v1"
SYNTHETIC_LABEL = "Synthetic benchmark fixture. Not a NileTech publication."
FIXED_PDF_TIME = time.strptime("2026-06-30", "%Y-%m-%d")

# about.md from niletech-0.1.6 (PUBLIC). Kept verbatim; its hash is checked when the
# archive is available and always recorded in the manifest.
ABOUT_MD_PATH = "documents/niletech/PUBLIC/PUBLIC/about.md"
ABOUT_MD_SHA256 = "see source-manifest.json"  # replaced at runtime from the vendored manifest
ABOUT_TITLE = "About NileTech Solutions"
ABOUT_INTRO = "NileTech Solutions operates from Cairo, Alexandria, Dubai."
ABOUT_SERVICES = [
    "Business Process Automation — Replace manual back-office steps with governed workflows.",
    "Custom Software Engineering — Build and maintain line-of-business systems end to end.",
    "Data Platform Engineering — Consolidate scattered data into a governed analytics layer.",
    "Systems Integration — Connect existing systems without replacing them.",
    "Cloud Migration — Move workloads with a documented rollback path at every stage.",
    "Managed Support — Ongoing operation and improvement of delivered systems.",
]
ABOUT_CONTACT = "General enquiries: hello@niletech.example"


@dataclass
class Fixture:
    id: str
    case: str
    filename: str
    media_type: str
    sources: list[str]
    notes: str


# --------------------------------------------------------------------------------------
# Sales aggregation (independent Decimal pass over the vendored JSONL)
# --------------------------------------------------------------------------------------


def _read_table(name: str) -> list[dict]:
    with gzip.open(SALES_DIR / "tables" / f"{name}.jsonl.gz", "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def money(x: Decimal) -> str:
    return f"{x:,.2f}"


MONTHS = {1: "January", 2: "February", 3: "March"}


def sales_q1() -> dict:
    orders = _read_table("orders")
    lines = _read_table("order_lines")
    products = {p["id"]: p for p in _read_table("products")}
    customers = {c["id"]: c for c in _read_table("customers")}
    q1 = [o for o in orders if "2026-01-01" <= o["order_date"] <= "2026-03-31"]
    fulfilled = [o for o in q1 if o["status"] == "FULFILLED"]
    fid = {o["id"] for o in fulfilled}

    by_month: dict[int, dict] = {
        m: {"orders": 0, "net": Decimal(0), "tax": Decimal(0), "gross": Decimal(0)} for m in MONTHS
    }
    by_region: dict[str, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for o in fulfilled:
        m = int(o["order_date"][5:7])
        b = by_month[m]
        b["orders"] += 1
        b["net"] += Decimal(o["subtotal"])
        b["tax"] += Decimal(o["tax"])
        b["gross"] += Decimal(o["total"])
        by_region[o["region"]][m] += Decimal(o["subtotal"])

    by_status: dict[str, dict] = {}
    for status in ("FULFILLED", "PENDING", "CANCELLED"):
        rows = [o for o in q1 if o["status"] == status]
        by_status[status] = {"orders": len(rows), "net": sum((Decimal(o["subtotal"]) for o in rows), Decimal(0))}

    by_product: dict[str, dict] = {
        p["id"]: {"sku": p["sku"], "name": p["name"], "tier": p["tier"], "units": 0, "net": Decimal(0)}
        for p in products.values()
    }
    for ln in lines:
        if ln["order_id"] in fid:
            bp = by_product[ln["product_id"]]
            bp["units"] += int(ln["quantity"])
            bp["net"] += Decimal(ln["line_total"])

    net_total = sum((b["net"] for b in by_month.values()), Decimal(0))
    # Cross-check against the frozen figures in docs/data/sales-report-q1-2026.md.
    assert net_total == Decimal("80934385.00"), net_total
    assert sum(b["orders"] for b in by_month.values()) == 291
    assert sum((b["tax"] for b in by_month.values()), Decimal(0)) == Decimal("11330813.90")
    assert sum((p["net"] for p in by_product.values()), Decimal(0)) == net_total
    assert sum((sum(r.values(), Decimal(0)) for r in by_region.values()), Decimal(0)) == net_total
    assert by_status["PENDING"]["net"] == Decimal("12605562.00")
    assert by_status["CANCELLED"]["net"] == Decimal("9447839.00")

    order_rows = sorted(fulfilled, key=lambda o: (o["order_date"], o["order_number"]))
    return {
        "by_month": by_month,
        "by_region": by_region,
        "by_status": by_status,
        "by_product": sorted(by_product.values(), key=lambda p: p["sku"]),
        "orders": order_rows,
        "orders_all": {o["order_number"]: o for o in orders},
        "lines": lines,
        "products": products,
        "customers": customers,
    }


# --------------------------------------------------------------------------------------
# PDF helpers (reportlab, invariant mode => byte-identical output)
# --------------------------------------------------------------------------------------


def _register_fonts() -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "DejaVuSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVuSans", str(FONT_REGULAR)))
        pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(FONT_BOLD)))


def _styles():
    from reportlab.lib.styles import ParagraphStyle

    base = ParagraphStyle("body", fontName="DejaVuSans", fontSize=10.5, leading=14, spaceAfter=6)
    return {
        "title": ParagraphStyle(
            "title", parent=base, fontName="DejaVuSans-Bold", fontSize=18, leading=22, spaceAfter=10
        ),
        "h2": ParagraphStyle(
            "h2", parent=base, fontName="DejaVuSans-Bold", fontSize=13, leading=17, spaceBefore=8, spaceAfter=4
        ),
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=11),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=11, spaceAfter=0),
    }


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _footer(label: str):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("DejaVuSans", 8)
        canvas.drawString(50, 30, f"{label} · Page {doc.page}")
        canvas.restoreState()

    return draw


def _doc(path: Path, title: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    return SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=50,
        bottomMargin=55,
        title=title,
        author="EAIOS parser benchmark",
        creator=GENERATOR_VERSION,
        producer=GENERATOR_VERSION,
        subject=SYNTHETIC_LABEL,
        invariant=1,
    )


# --------------------------------------------------------------------------------------
# F1: digital, single column
# --------------------------------------------------------------------------------------

DELIVERY_TITLE = "How we deliver"
DELIVERY_NOTE = "This section is synthetic text written for the parser benchmark."
DELIVERY_PARAS = [
    "Every engagement starts with a short discovery phase. The team maps the current process, "
    "lists the systems it touches and agrees with the client which steps must never change.",
    "Work is delivered in two-week increments. Each increment ends with a demonstration on the "
    "client's own data and a written note of what changed, what was tested and what is still open.",
]
DELIVERY_STEPS = [
    "1. Discovery: map the process, the systems and the owners.",
    "2. Design: agree the target workflow and its controls.",
    "3. Build: deliver in increments with tests on real data.",
    "4. Handover: train operators and document the runbook.",
]
DIGITAL_FOOTER = "NileTech Solutions — company overview (synthetic fixture)"


def build_digital(path: Path) -> dict:
    from reportlab.platypus import Paragraph, Spacer

    s = _styles()
    story = [Paragraph(_esc(ABOUT_TITLE), s["title"]), Paragraph(_esc(ABOUT_INTRO), s["body"])]
    story.append(Paragraph("Services", s["h2"]))
    for item in ABOUT_SERVICES:
        story.append(Paragraph(_esc(item), s["body"], bulletText="•"))
    story.append(Paragraph("Contact", s["h2"]))
    story.append(Paragraph(_esc(ABOUT_CONTACT), s["body"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(_esc(DELIVERY_TITLE), s["h2"]))
    story.append(Paragraph(_esc(DELIVERY_NOTE), s["small"]))
    for p in DELIVERY_PARAS:
        story.append(Paragraph(_esc(p), s["body"]))
    for step in DELIVERY_STEPS:
        story.append(Paragraph(_esc(step), s["body"]))
    _doc(path, "NileTech company overview").build(
        story, onFirstPage=_footer(DIGITAL_FOOTER), onLaterPages=_footer(DIGITAL_FOOTER)
    )

    blocks = [
        ABOUT_TITLE,
        ABOUT_INTRO,
        "Services",
        *ABOUT_SERVICES,
        "Contact",
        ABOUT_CONTACT,
        DELIVERY_TITLE,
        DELIVERY_NOTE,
        *DELIVERY_PARAS,
        *DELIVERY_STEPS,
    ]
    return {
        "text_blocks": blocks,
        "reading_order": [_anchor(b) for b in blocks],
        "furniture": [f"{DIGITAL_FOOTER} · Page 1"],
        "tables": [],
        "headings": [ABOUT_TITLE, "Services", "Contact", DELIVERY_TITLE],
    }


def _table_lines(header_rows: list[list[str]], rows: list[list[str]]) -> list[str]:
    """Plain-text form of a table for the gold text: one line per row, cells joined by spaces.

    A header repeated after a page break (table T4) is printed twice on the page but listed once
    here; that costs a parser a few characters of text similarity at most.
    """
    return [" ".join(c for c in r if c) for r in [*header_rows, *rows]]


def _anchor(text: str, words: int = 6) -> str:
    return " ".join(text.split()[:words])


# --------------------------------------------------------------------------------------
# F2: two columns, manual layout, content stream deliberately NOT in reading order
# --------------------------------------------------------------------------------------

NEWS_TITLE = "NileTech Field Notes — Autumn 2026"
NEWS_SUBTITLE = SYNTHETIC_LABEL
NEWS_FOOTER = "NileTech Field Notes (synthetic fixture) · Page 1"
NEWS_NOTE_HEAD = "Editor's note"
NEWS_NOTE_BODY = (
    "This full-width note sits below both columns and should be read last. Figures in these "
    "notes are illustrative and are not drawn from the NileTech sales baseline."
)
NEWS_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "Delivery in Alexandria",
        [
            "The Alexandria delivery team completed the warehouse integration for a regional "
            "distributor in eleven weeks. The project replaced three spreadsheets and a nightly email "
            "with a governed workflow that posts stock movements to the distributor's ledger within minutes.",
            "The team kept the old process running in parallel for two full month-end closes. Only "
            "after both closes reconciled to the cent did the distributor switch off the spreadsheets.",
        ],
    ),
    (
        "Data platform milestones",
        [
            "Our data platform practice now runs a shared analytics layer for four clients. Each client "
            "keeps its own warehouse schema, and every dashboard reads from views that carry the "
            "client's access rules.",
            "Query volume tripled between July and September. Median dashboard load time stayed under "
            "two seconds because the heaviest reports are precomputed overnight and refreshed "
            "incrementally during the day.",
        ],
    ),
    (
        "People and offices",
        [
            "The Dubai office moved to a larger floor in Business Bay in September. The Cairo office "
            "opened a second training room, which hosts the monthly engineering guild and the "
            "new-joiner programme.",
            "Fourteen engineers completed the internal cloud migration certification this quarter. The "
            "programme covers rollback planning, cost tagging and the runbook format that every "
            "migration now follows.",
        ],
    ),
    (
        "What comes next",
        [
            "In the next quarter the integration team will publish a reference architecture for "
            "connecting legacy accounting systems without replacing them. Drafts will be reviewed by "
            "two client engineering leads before release.",
            "Questions about these notes can be sent to the internal communications team. External "
            "enquiries should use hello@niletech.example.",
        ],
    ),
]


def build_two_column(path: Path) -> dict:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen.canvas import Canvas

    width, height = A4
    left, right_margin, gutter = 50, 50, 24
    col_w = (width - left - right_margin - gutter) / 2
    top_y = height - 130
    bottom_y = top_y - 332  # columns fill the upper part; a full-width note follows
    body_size, body_lead = 10, 13.5
    head_size, head_lead = 11.5, 16

    # Lay out lines into columns in reading order.
    lines: list[tuple[str, str, float]] = []  # (text, font, size) with para separators
    para_starts: list[int] = []
    for heading, paras in NEWS_SECTIONS:
        lines.append(("\x00gap", "", 6))
        para_starts.append(len(lines))
        lines.append((heading, "DejaVuSans-Bold", head_size))
        for p in paras:
            para_starts.append(len(lines))
            for ln in simpleSplit(p, "DejaVuSans", body_size, col_w):
                lines.append((ln, "DejaVuSans", body_size))
            lines.append(("\x00gap", "", 5))

    placed: list[tuple[int, float, float, str, str, float]] = []  # (col, x, y, text, font, size)
    col, y = 0, top_y
    continuation_line: str | None = None
    for idx, (text, font, size) in enumerate(lines):
        lead = 0.0 if text.startswith("\x00") else (head_lead if font.endswith("Bold") else body_lead)
        if text.startswith("\x00"):
            y -= size
            continue
        if y - lead < bottom_y:
            if col == 1:
                raise RuntimeError("two-column fixture overflowed the page")
            col, y = 1, top_y
            if idx not in para_starts:
                continuation_line = text
        y -= lead
        x = left + col * (col_w + gutter)
        placed.append((col, x, y, text, font, size))
    if continuation_line is None:
        raise RuntimeError("expected a paragraph to continue across the column break")

    c = Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle("NileTech Field Notes")
    c.setAuthor("EAIOS parser benchmark")
    c.setCreator(GENERATOR_VERSION)
    c.setProducer(GENERATOR_VERSION)
    c.setSubject(SYNTHETIC_LABEL)

    def draw_lines(which: int) -> None:
        for col_i, x, yy, text, font, size in placed:
            if col_i == which:
                c.setFont(font, size)
                c.drawString(x, yy, text)

    # Deliberately scrambled drawing order: footer, full-width note, right column, title,
    # left column.
    c.setFont("DejaVuSans", 8)
    c.drawString(left, 30, NEWS_FOOTER)
    note_lines = simpleSplit(NEWS_NOTE_BODY, "DejaVuSans", body_size, width - left - right_margin)
    ny = bottom_y - 40
    c.setLineWidth(0.6)
    c.rect(
        left - 6,
        ny - 16 - len(note_lines) * body_lead - 8,
        width - left - right_margin + 12,
        16 + len(note_lines) * body_lead + 22,
    )
    c.setFont("DejaVuSans-Bold", head_size)
    c.drawString(left, ny, NEWS_NOTE_HEAD)
    ny -= 16
    c.setFont("DejaVuSans", body_size)
    for ln in note_lines:
        ny -= body_lead
        c.drawString(left, ny, ln)
    draw_lines(1)
    c.setFont("DejaVuSans-Bold", 20)
    c.drawString(left, height - 70, NEWS_TITLE)
    c.setFont("DejaVuSans", 9)
    c.drawString(left, height - 90, NEWS_SUBTITLE)
    c.setLineWidth(0.6)
    c.line(left, height - 104, width - right_margin, height - 104)
    draw_lines(0)
    c.showPage()
    c.save()

    blocks = [NEWS_TITLE, NEWS_SUBTITLE]
    for heading, paras in NEWS_SECTIONS:
        blocks.append(heading)
        blocks.extend(paras)
    blocks += [NEWS_NOTE_HEAD, NEWS_NOTE_BODY]
    order = [_anchor(b) for b in blocks]
    # The continuation anchor must come right after the paragraph it continues.
    cont = _anchor(continuation_line)
    host = next(i for i, b in enumerate(blocks) if " ".join(continuation_line.split()) in " ".join(b.split()))
    order.insert(host + 1, cont)
    return {
        "text_blocks": blocks,
        "reading_order": order,
        "furniture": [NEWS_FOOTER],
        "tables": [],
        "headings": [NEWS_TITLE] + [h for h, _ in NEWS_SECTIONS] + [NEWS_NOTE_HEAD],
        "layout": {
            "columns": 2,
            "full_width_after_columns": NEWS_NOTE_HEAD,
            "content_stream_order": "footer, full-width note, right column, title, left column",
            "column_break_inside_paragraph": continuation_line,
        },
    }


# --------------------------------------------------------------------------------------
# F3: table-heavy (four table styles, one split across a page break)
# --------------------------------------------------------------------------------------

TABLE_TITLE = "NileTech Q1 2026 sales tables"
TABLE_INTRO = (
    "Figures cover orders dated 2026-01-01 to 2026-03-31. Net sales exclude tax; tax is 14%. "
    "Amounts are in USD. Values are derived from the niletech-0.1.6 sales baseline."
)
TABLE_FOOTER = "Q1 2026 sales tables (synthetic fixture)"


def build_table_heavy(path: Path, sales: dict) -> dict:
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    s = _styles()
    story = [
        Paragraph(_esc(TABLE_TITLE), s["title"]),
        Paragraph(_esc(SYNTHETIC_LABEL), s["small"]),
        Paragraph(_esc(TABLE_INTRO), s["body"]),
        Spacer(1, 6),
    ]
    gold_tables = []
    blocks = [TABLE_TITLE, SYNTHETIC_LABEL, TABLE_INTRO]

    # T1: full grid
    t1_title = "Table 1. Fulfilled sales by month"
    t1_head = ["Month", "Orders", "Net sales (USD)", "Tax (USD)", "Gross (USD)"]
    t1_rows = []
    tot = {"orders": 0, "net": Decimal(0), "tax": Decimal(0), "gross": Decimal(0)}
    for m, name in MONTHS.items():
        b = sales["by_month"][m]
        t1_rows.append([f"{name} 2026", str(b["orders"]), money(b["net"]), money(b["tax"]), money(b["gross"])])
        for k in tot:
            tot[k] += b[k]
    t1_rows.append(["Total", str(tot["orders"]), money(tot["net"]), money(tot["tax"]), money(tot["gross"])])
    story.append(Paragraph(_esc(t1_title), s["h2"]))
    t1 = Table([t1_head, *t1_rows], colWidths=[100, 55, 110, 100, 110])
    t1.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVuSans", 9),
                ("FONT", (0, 0), (-1, 0), "DejaVuSans-Bold", 9),
                ("FONT", (0, -1), (-1, -1), "DejaVuSans-Bold", 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDDDDD")),
            ]
        )
    )
    story += [t1, Spacer(1, 10)]
    gold_tables.append({"id": "T1", "title": t1_title, "style": "full grid", "header_rows": [t1_head], "rows": t1_rows})
    blocks += [t1_title, *_table_lines([t1_head], t1_rows)]

    # T2: borderless
    t2_title = "Table 2. Q1 2026 orders by status"
    t2_head = ["Status", "Orders", "Net value (USD)"]
    t2_rows = []
    all_n, all_v = 0, Decimal(0)
    for st in ("FULFILLED", "PENDING", "CANCELLED"):
        b = sales["by_status"][st]
        t2_rows.append([st, str(b["orders"]), money(b["net"])])
        all_n += b["orders"]
        all_v += b["net"]
    t2_rows.append(["All statuses", str(all_n), money(all_v)])
    story.append(Paragraph(_esc(t2_title), s["h2"]))
    t2 = Table([t2_head, *t2_rows], colWidths=[120, 70, 130], hAlign="LEFT")
    t2.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVuSans", 9),
                ("FONT", (0, 0), (-1, 0), "DejaVuSans-Bold", 9),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ]
        )
    )
    story += [t2, Spacer(1, 10)]
    gold_tables.append(
        {"id": "T2", "title": t2_title, "style": "borderless", "header_rows": [t2_head], "rows": t2_rows}
    )
    blocks += [t2_title, *_table_lines([t2_head], t2_rows)]

    # T3: two-level header with spans
    t3_title = "Table 3. Fulfilled net sales by region and month (USD)"
    t3_top = ["Region", "Q1 2026", "", "", ""]
    t3_head = ["", "January", "February", "March", "Quarter"]
    t3_rows = []
    for region in sorted(sales["by_region"]):
        r = sales["by_region"][region]
        q = sum((r[m] for m in MONTHS), Decimal(0))
        t3_rows.append([region, money(r[1]), money(r[2]), money(r[3]), money(q)])
    story.append(Paragraph(_esc(t3_title), s["h2"]))
    t3 = Table([t3_top, t3_head, *t3_rows], colWidths=[110, 95, 95, 95, 100])
    t3.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "DejaVuSans", 9),
                ("FONT", (0, 0), (-1, 1), "DejaVuSans-Bold", 9),
                ("SPAN", (0, 0), (0, 1)),
                ("SPAN", (1, 0), (4, 0)),
                ("ALIGN", (1, 0), (4, 0), "CENTER"),
                ("VALIGN", (0, 0), (0, 1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("ALIGN", (1, 2), (-1, -1), "RIGHT"),
            ]
        )
    )
    story += [t3, Spacer(1, 10)]
    gold_tables.append(
        {
            "id": "T3",
            "title": t3_title,
            "style": "grid with merged two-level header",
            "header_rows": [t3_top, ["Region", *t3_head[1:]]],
            "rows": t3_rows,
        }
    )
    blocks += [t3_title, *_table_lines([t3_top, t3_head], t3_rows)]

    # T4: horizontal rules + zebra shading, long enough to split across pages (header repeats)
    t4_title = "Table 4. Product sales from fulfilled Q1 2026 orders"
    t4_head = ["SKU", "Product", "Tier", "Units", "Net sales (USD)"]
    t4_rows = [[p["sku"], p["name"], p["tier"], str(p["units"]), money(p["net"])] for p in sales["by_product"]]
    story.append(Paragraph(_esc(t4_title), s["h2"]))
    t4 = Table([t4_head, *t4_rows], colWidths=[60, 200, 75, 45, 105], repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, -1), "DejaVuSans", 9),
        ("FONT", (0, 0), (-1, 0), "DejaVuSans-Bold", 9),
        ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black),
        ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
    ]
    for i in range(1, len(t4_rows) + 1):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#EEEEEE")))
    t4.setStyle(TableStyle(style))
    story.append(t4)
    gold_tables.append(
        {
            "id": "T4",
            "title": t4_title,
            "style": "horizontal rules, zebra, split across pages with repeated header",
            "header_rows": [t4_head],
            "rows": t4_rows,
        }
    )
    blocks += [t4_title, *_table_lines([t4_head], t4_rows)]

    _doc(path, TABLE_TITLE).build(story, onFirstPage=_footer(TABLE_FOOTER), onLaterPages=_footer(TABLE_FOOTER))

    order = [_anchor(TABLE_TITLE), _anchor(SYNTHETIC_LABEL), _anchor(TABLE_INTRO)]
    for t in gold_tables:
        order.append(_anchor(t["title"]))
        order.append(" ".join(t["rows"][0]))  # first body row, cells in order
        order.append(" ".join(t["rows"][-1]))  # last body row
    return {
        "text_blocks": blocks,  # table rows appear once, cells joined by spaces
        "reading_order": order,
        "furniture": [f"{TABLE_FOOTER} · Page 1", f"{TABLE_FOOTER} · Page 2"],
        "tables": gold_tables,
        "headings": [TABLE_TITLE] + [t["title"] for t in gold_tables],
    }


# --------------------------------------------------------------------------------------
# F4: scanned delivery note (image-only PDF, rotated and speckled)
# --------------------------------------------------------------------------------------

SCAN_ORDER = "SO-001809"
SCAN_TERMS = (
    "Goods remain the property of NileTech Solutions until paid in full. "
    "Report any damage or shortage within 7 days of delivery."
)
SCAN_FOOTER = "Synthetic benchmark fixture — simulated scan of a printed page."


def build_scanned(path: Path, sales: dict) -> dict:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    order = sales["orders_all"][SCAN_ORDER]
    customer = sales["customers"][order["customer_id"]]
    olines = sorted(
        (ln for ln in sales["lines"] if ln["order_id"] == order["id"]),
        key=lambda ln: sales["products"][ln["product_id"]]["sku"],
    )
    head = ["SKU", "Description", "Qty", "Unit price (USD)", "Line total (USD)"]
    rows = []
    for ln in olines:
        p = sales["products"][ln["product_id"]]
        rows.append(
            [
                p["sku"],
                p["name"],
                str(ln["quantity"]),
                money(Decimal(ln["unit_price"])),
                money(Decimal(ln["line_total"])),
            ]
        )
    assert money(Decimal(order["subtotal"])) == "550,497.00"

    dpi = 200
    w, h = int(8.27 * dpi), int(11.69 * dpi)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    f = lambda size, bold=False: ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)  # noqa: E731
    x0, y = 150, 160
    header_lines = [
        ("NileTech Solutions", f(64, True)),
        ("DELIVERY NOTE", f(44, True)),
        (f"Delivery note no. DN-{SCAN_ORDER[3:]}", f(32)),
        (f"Order: {SCAN_ORDER}    Order date: {order['order_date']}", f(32)),
        (f"Customer: {customer['name']}", f(32)),
    ]
    text_blocks = []
    for text, font in header_lines:
        d.text((x0, y), text, font=font, fill=0)
        text_blocks.append(text)
        y += int(font.size * 1.6)
    y += 30
    cols = [x0, x0 + 180, x0 + 690, x0 + 780, x0 + 1075, w - 150]
    row_h = 70
    tf = f(28)
    tfb = f(26, True)
    table_top = y
    for r_i, row in enumerate([head, *rows]):
        for c_i, cell in enumerate(row):
            d.text((cols[c_i] + 12, y + 18), cell, font=tfb if r_i == 0 else tf, fill=0)
        y += row_h
    for yy in range(table_top, y + 1, row_h):
        d.line([(cols[0], yy), (cols[-1], yy)], fill=0, width=3)
    for xx in cols:
        d.line([(xx, table_top), (xx, y)], fill=0, width=3)
    y += 50
    totals = [
        f"Subtotal: USD {money(Decimal(order['subtotal']))}",
        f"Tax 14%: USD {money(Decimal(order['tax']))}",
        f"Total: USD {money(Decimal(order['total']))}",
    ]
    for t in totals:
        d.text((x0 + 860, y), t, font=f(32, t.startswith("Total")), fill=0)
        y += 55
    y += 60
    # Wrap the terms paragraph by hand at a fixed width.
    words, line = SCAN_TERMS.split(), ""
    tfont = f(30)
    for word in words:
        trial = f"{line} {word}".strip()
        if d.textlength(trial, font=tfont) > w - 2 * x0:
            d.text((x0, y), line, font=tfont, fill=0)
            y += 48
            line = word
        else:
            line = trial
    d.text((x0, y), line, font=tfont, fill=0)
    d.text((x0, h - 150), SCAN_FOOTER, font=f(24), fill=0)

    rng = random.Random(20260630)
    img = img.rotate(0.8, resample=Image.Resampling.BICUBIC, fillcolor=255)
    px = img.load()
    for _ in range(9000):  # speckle noise
        px[rng.randrange(w), rng.randrange(h)] = rng.choice((0, 90, 160))
    img = img.filter(ImageFilter.GaussianBlur(0.7))
    img.save(
        path,
        "PDF",
        resolution=dpi,
        title="Scanned delivery note",
        author="EAIOS parser benchmark",
        creator=GENERATOR_VERSION,
        producer=GENERATOR_VERSION,
        subject=SYNTHETIC_LABEL,
        creationDate=FIXED_PDF_TIME,
        modDate=FIXED_PDF_TIME,
    )  # Pillow defaults to "now"

    blocks = [*text_blocks, *_table_lines([head], rows), *totals, SCAN_TERMS]
    order_anchors = [_anchor(b) for b in text_blocks] + [" ".join(head), " ".join(rows[0]), " ".join(rows[-1])]
    order_anchors += [_anchor(t) for t in totals] + [_anchor(SCAN_TERMS)]
    return {
        "text_blocks": blocks,
        "reading_order": order_anchors,
        "furniture": [SCAN_FOOTER],
        "tables": [
            {
                "id": "S1",
                "title": "Delivery note lines",
                "style": "full grid (raster)",
                "header_rows": [head],
                "rows": rows,
            }
        ],
        "headings": ["NileTech Solutions", "DELIVERY NOTE"],
        "layout": {"image_only": True, "dpi": dpi, "rotation_degrees": 0.8, "noise": "9000 speckles, blur 0.7"},
    }


# --------------------------------------------------------------------------------------
# Spreadsheets
# --------------------------------------------------------------------------------------

CSV_TRICKY_HEADER = ["customer_code", "name", "country", "city", "credit_limit_usd", "since_date", "notes"]


def build_csv_tricky(path: Path, sales: dict) -> dict:
    names = [c["name"] for c in sorted(sales["customers"].values(), key=lambda c: c["name"])][:2]
    rows = [
        ["00017", names[0], "EG", "Cairo", "1,250,000.00", "2021-04-14", "Key account"],
        ["00102", "Nile Delta Trading, S.A.E.", "EG", "Alexandria", "98000.50", "2022-11-03", 'Prefers "net 45" terms'],
        ["00230", "شركة النيل للتقنية", "EG", "القاهرة", "45000", "2023-01-09", "Arabic name and city"],
        ["01005", "Zürich Analytics GmbH", "CH", "Zürich", "-1200.00", "2020-06-30", "Line one\nLine two"],
        ["00999", "Café Système SARL", "FR", "Paris", "", "2024-02-29", ""],
        ["00042", "Gulf Ventures LLC 🚀", "AE", "Dubai", "300000", "2019-12-31", "  leading and trailing spaces  "],
        ["00007", "=1+1 Holdings", "AE", "Abu Dhabi", "0", "2025-01-01", "Starts with = (formula-injection text)"],
        ["00311", names[1], "SA", "Riyadh", "75,500", "2018-07-15", "comma, inside; and semicolon"],
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(CSV_TRICKY_HEADER)
    w.writerows(rows)
    path.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    return {
        "kind": "csv",
        "encoding": "utf-8 with BOM",
        "delimiter": ",",
        "line_terminator": "CRLF",
        "rows": [CSV_TRICKY_HEADER, *rows],
        "checks": {
            "leading_zero_codes": [r[0] for r in rows],
            "non_ascii": [
                "شركة النيل للتقنية",
                "القاهرة",
                "Zürich Analytics GmbH",
                "Café Système SARL",
                "Gulf Ventures LLC 🚀",
            ],
            "embedded_newline": "Line one\nLine two",
            "doubled_quotes": 'Prefers "net 45" terms',
            "formula_text": "=1+1 Holdings",
            "empty_fields": 2,
        },
    }


CSV_EU_HEADER = ["invoice_number", "customer", "amount_eur", "issue_date", "status"]


def build_csv_semicolon(path: Path) -> dict:
    rows = [
        ["INV-900101", "Müller & Söhne KG", "1.234,50", "03.02.2026", "PAID"],
        ["INV-900102", "Café Lumière", "980,00", "10.02.2026", "OPEN"],
        ["INV-900103", "Señor Datos S.L.", "12.000,75", "28.02.2026", "PAID"],
        ["INV-900104", "Øresund Logistik A/S", "-150,25", "01.03.2026", "CREDIT"],
        ["INV-900105", "Price; quoted delimiter Ltd", "5,00", "15.03.2026", "OPEN"],
        ["INV-900106", "€uro Systems SARL", "7.777,77", "31.03.2026", "OVERDUE"],
    ]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(CSV_EU_HEADER)
    w.writerows(rows)
    path.write_bytes(buf.getvalue().encode("cp1252"))
    return {
        "kind": "csv",
        "encoding": "cp1252 (no BOM)",
        "delimiter": ";",
        "line_terminator": "LF",
        "rows": [CSV_EU_HEADER, *rows],
        "checks": {
            "non_ascii": [
                "Müller & Söhne KG",
                "Café Lumière",
                "Señor Datos S.L.",
                "Øresund Logistik A/S",
                "€uro Systems SARL",
            ],
            "decimal_comma": ["1.234,50", "12.000,75", "-150,25"],
            "quoted_delimiter": "Price; quoted delimiter Ltd",
        },
    }


def build_workbook(path: Path, sales: dict) -> dict:
    from datetime import datetime

    import xlsxwriter

    wb = xlsxwriter.Workbook(str(path))
    wb.set_properties(
        {
            "title": "Q1 2026 workbook (synthetic fixture)",
            "author": "EAIOS parser benchmark",
            "comments": SYNTHETIC_LABEL,
            "created": datetime(2026, 6, 30),
        }
    )
    bold = wb.add_format({"bold": True})
    money_f = wb.add_format({"num_format": "#,##0.00"})
    pct = wb.add_format({"num_format": "0.0%"})
    datef = wb.add_format({"num_format": "yyyy-mm-dd"})
    wrap = wb.add_format({"text_wrap": True})
    title_f = wb.add_format({"bold": True, "font_size": 14, "align": "center"})

    gold_sheets = []

    def cell(ref, value, typ, display=None, formula=None):
        return ref, {
            "value": value,
            "type": typ,
            "display": display if display is not None else value,
            "formula": formula,
        }

    # Orders sheet first (Summary formulas reference it)
    orders = sales["orders"]
    n = len(orders)
    ws_o_cells = {}
    o_head = ["order_number", "month", "order_date", "region", "net_sales", "tax", "gross"]
    # Summary is created first so it is the first tab.
    ws_s = wb.add_worksheet("Summary")
    ws_o = wb.add_worksheet("Orders Q1")
    ws_n = wb.add_worksheet("Notes")
    ws_l = wb.add_worksheet("Lookup")
    ws_l.hide()

    for c_i, h in enumerate(o_head):
        ws_o.write(0, c_i, h, bold)
        ref = xlsxwriter.utility.xl_rowcol_to_cell(0, c_i)
        k, v = cell(ref, h, "str")
        ws_o_cells[k] = v
    for r_i, o in enumerate(orders, start=1):
        mname = MONTHS[int(o["order_date"][5:7])]
        dt = datetime.fromisoformat(o["order_date"])
        vals = [
            (o["order_number"], "str", None),
            (mname, "str", None),
            (o["order_date"], "date", dt),
            (o["region"], "str", None),
            (o["subtotal"], "num", Decimal(o["subtotal"])),
            (o["tax"], "num", Decimal(o["tax"])),
            (o["total"], "num", Decimal(o["total"])),
        ]
        for c_i, (gold_v, typ, raw) in enumerate(vals):
            ref = xlsxwriter.utility.xl_rowcol_to_cell(r_i, c_i)
            if typ == "date":
                ws_o.write_datetime(r_i, c_i, raw, datef)
                k, v = cell(ref, gold_v, "date")
            elif typ == "num":
                ws_o.write_number(r_i, c_i, float(raw), money_f)
                k, v = cell(ref, gold_v, "num", display=money(raw))
            else:
                ws_o.write_string(r_i, c_i, gold_v)
                k, v = cell(ref, gold_v, "str")
            ws_o_cells[k] = v
    ws_o.set_column(0, 6, 14)

    # Summary sheet: merged title, formulas with cached values
    s_cells = {}
    title = "NileTech Q1 2026 fulfilled sales (synthetic workbook fixture)"
    ws_s.merge_range("A1:D1", title, title_f)
    s_cells.update([cell("A1", title, "str")])
    heads = ["Month", "Orders", "Net sales (USD)", "Share of quarter"]
    for c_i, h in enumerate(heads):
        ws_s.write(2, c_i, h, bold)
        s_cells.update([cell(xlsxwriter.utility.xl_rowcol_to_cell(2, c_i), h, "str")])
    last = n + 1
    total_net = sum((sales["by_month"][m]["net"] for m in MONTHS), Decimal(0))
    for i, (m, name) in enumerate(MONTHS.items()):
        r = 3 + i
        b = sales["by_month"][m]
        ws_s.write_string(r, 0, name)
        f_orders = f"=COUNTIF('Orders Q1'!B2:B{last},A{r + 1})"
        f_net = f"=SUMIF('Orders Q1'!B2:B{last},A{r + 1},'Orders Q1'!E2:E{last})"
        f_share = f"=C{r + 1}/$C$7"
        share = b["net"] / total_net
        ws_s.write_formula(r, 1, f_orders, None, b["orders"])
        ws_s.write_formula(r, 2, f_net, money_f, float(b["net"]))
        ws_s.write_formula(r, 3, f_share, pct, float(share))
        s_cells.update(
            [
                cell(f"A{r + 1}", name, "str"),
                cell(f"B{r + 1}", str(b["orders"]), "num", formula=f_orders),
                cell(f"C{r + 1}", f"{b['net']:.2f}", "num", display=money(b["net"]), formula=f_net),
                cell(f"D{r + 1}", f"{float(share):.10f}", "num", display=f"{float(share) * 100:.1f}%", formula=f_share),
            ]
        )
    tot_orders = sum(sales["by_month"][m]["orders"] for m in MONTHS)
    ws_s.write_string(6, 0, "Total", bold)
    ws_s.write_formula(6, 1, "=SUM(B4:B6)", bold, tot_orders)
    ws_s.write_formula(6, 2, "=SUM(C4:C6)", money_f, float(total_net))
    ws_s.write_formula(6, 3, "=SUM(D4:D6)", pct, 1.0)
    s_cells.update(
        [
            cell("A7", "Total", "str"),
            cell("B7", str(tot_orders), "num", formula="=SUM(B4:B6)"),
            cell("C7", f"{total_net:.2f}", "num", display=money(total_net), formula="=SUM(C4:C6)"),
            cell("D7", "1.0000000000", "num", display="100.0%", formula="=SUM(D4:D6)"),
        ]
    )
    ws_s.set_column(0, 3, 20)

    # Notes sheet
    note_text = "Figures exclude PENDING and CANCELLED orders.\nTax is 14%."
    n_cells = {}
    ws_n.write_string(0, 0, "Notes", bold)
    ws_n.write_string(1, 0, note_text, wrap)
    ws_n.write_string(2, 0, "ملاحظة: الأرقام بالدولار الأمريكي")
    ws_n.write_string(3, 0, "00123")
    ws_n.write_boolean(4, 0, True)
    ws_n.write_string(6, 0, "end")
    n_cells.update(
        [
            cell("A1", "Notes", "str"),
            cell("A2", note_text, "str"),
            cell("A3", "ملاحظة: الأرقام بالدولار الأمريكي", "str"),
            cell("A4", "00123", "str"),
            cell("A5", "TRUE", "bool"),
            cell("A7", "end", "str"),
        ]
    )
    ws_n.set_column(0, 0, 50)

    # Hidden lookup sheet
    l_cells = {}
    ws_l.write_string(0, 0, "month_number", bold)
    ws_l.write_string(0, 1, "month_name", bold)
    l_cells.update([cell("A1", "month_number", "str"), cell("B1", "month_name", "str")])
    for i, (m, name) in enumerate(MONTHS.items(), start=1):
        ws_l.write_number(i, 0, m)
        ws_l.write_string(i, 1, name)
        l_cells.update([cell(f"A{i + 1}", str(m), "num"), cell(f"B{i + 1}", name, "str")])

    wb.close()
    gold_sheets = [
        {"name": "Summary", "hidden": False, "merged": ["A1:D1"], "cells": s_cells},
        {"name": "Orders Q1", "hidden": False, "merged": [], "cells": ws_o_cells},
        {"name": "Notes", "hidden": False, "merged": [], "cells": n_cells},
        {"name": "Lookup", "hidden": True, "merged": [], "cells": l_cells},
    ]
    return {"kind": "xlsx", "sheets": gold_sheets}


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------

FIXTURES = [
    Fixture(
        "digital-company-overview",
        "digital_pdf",
        "digital-company-overview.pdf",
        "application/pdf",
        ["baseline-public", "synthetic"],
        "about.md verbatim plus a labelled synthetic section; single column; logical content order",
    ),
    Fixture(
        "two-column-newsletter",
        "two_column_pdf",
        "two-column-newsletter.pdf",
        "application/pdf",
        ["synthetic"],
        "two columns; a paragraph continues across the column break; content stream drawn out of reading order",
    ),
    Fixture(
        "table-heavy-sales-q1-2026",
        "table_heavy_pdf",
        "table-heavy-sales-q1-2026.pdf",
        "application/pdf",
        ["baseline-sales", "synthetic"],
        "four table styles: full grid, borderless, merged two-level header, zebra table split over a page break",
    ),
    Fixture(
        "scanned-delivery-note",
        "scanned_pdf",
        "scanned-delivery-note.pdf",
        "application/pdf",
        ["baseline-sales", "synthetic"],
        "image-only page at 200 dpi, rotated 0.8 degrees, speckle noise and blur; no text layer",
    ),
    Fixture(
        "tricky-customers-csv",
        "csv",
        "tricky-customers.csv",
        "text/csv",
        ["baseline-sales", "synthetic"],
        "UTF-8 BOM, CRLF, quoted commas, doubled quotes, embedded newline, Arabic, emoji, leading zeros, formula-like text",
    ),
    Fixture(
        "semicolon-cp1252-csv",
        "csv",
        "semicolon-cp1252.csv",
        "text/csv",
        ["synthetic"],
        "semicolon delimiter, cp1252 encoding (euro sign 0x80), decimal commas, quoted delimiter",
    ),
    Fixture(
        "multi-sheet-workbook",
        "xlsx",
        "multi-sheet-q1.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ["baseline-sales", "synthetic"],
        "four sheets (one hidden), merged title, formulas with cached values, typed dates, wrapped text, Arabic, text-number",
    ),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(out: Path) -> dict:
    _register_fonts()
    files, gold_dir = out / "files", out / "gold"
    for d in (files, gold_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
    sales = sales_q1()
    builders = {
        "digital-company-overview": lambda p: build_digital(p),
        "two-column-newsletter": lambda p: build_two_column(p),
        "table-heavy-sales-q1-2026": lambda p: build_table_heavy(p, sales),
        "scanned-delivery-note": lambda p: build_scanned(p, sales),
        "tricky-customers-csv": lambda p: build_csv_tricky(p, sales),
        "semicolon-cp1252-csv": lambda p: build_csv_semicolon(p),
        "multi-sheet-workbook": lambda p: build_workbook(p, sales),
    }
    source_manifest = json.loads((SALES_DIR / "source-manifest.json").read_text())
    entries = []
    for fx in FIXTURES:
        p = files / fx.filename
        gold = builders[fx.id](p)
        gold = {"fixture": fx.id, "case": fx.case, "file": fx.filename, "file_sha256": sha256(p), **gold}
        if fx.media_type == "application/pdf":
            gold["text"] = "\n\n".join(gold["text_blocks"])
        gold_path = gold_dir / f"{fx.id}.json"
        gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        entries.append(
            {
                "id": fx.id,
                "case": fx.case,
                "file": f"files/{fx.filename}",
                "media_type": fx.media_type,
                "bytes": p.stat().st_size,
                "sha256": sha256(p),
                "gold": f"gold/{fx.id}.json",
                "gold_sha256": sha256(gold_path),
                "sources": fx.sources,
                "notes": fx.notes,
                "label": "SYNTHETIC",
            }
        )
    manifest = {
        "generator": GENERATOR_VERSION,
        "generator_script": "ml/parser-benchmark/fixtures/generate.py",
        "label": SYNTHETIC_LABEL,
        "provenance": {
            "baseline-public": {
                "archive": "niletech-0.1.6.zip (owner-supplied, outside the repository)",
                "archive_sha256": "e220664ca64a21a78450fb1abcc649c45e1038a18064bc33aabadff866fb42d3",
                "path": ABOUT_MD_PATH,
                "sha256": source_manifest["file_sha256"][ABOUT_MD_PATH],
                "classification": "PUBLIC",
            },
            "baseline-sales": {
                "path": "backend/fixtures/business/niletech-0.1.6/tables",
                "normalized_fingerprint": "e5715a94f93ed42dd31b3f00337af4bb591495a0cfd96f7a5a8ca90555bef127",
                "cross_checks": "Q1 2026 net 80,934,385.00 / 291 orders / tax 11,330,813.90 / pending 12,605,562.00 / cancelled 9,447,839.00",
            },
            "synthetic": "Text written for this benchmark in generate.py; not a NileTech publication.",
            "excluded": "INTERNAL, CONFIDENTIAL and RESTRICTED baseline documents (public repository).",
        },
        "fonts": {
            "DejaVuSans.ttf": sha256(FONT_REGULAR),
            "DejaVuSans-Bold.ttf": sha256(FONT_BOLD),
        },
        "fixtures": entries,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="regenerate in a temp dir and compare with committed files")
    args = ap.parse_args(argv)
    if not args.check:
        m = generate(HERE)
        for e in m["fixtures"]:
            print(f"{e['sha256']}  {e['file']}  ({e['bytes']} bytes)")
        return 0
    committed = json.loads((HERE / "manifest.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        fresh = generate(Path(tmp))
    bad = 0
    for a, b in zip(committed["fixtures"], fresh["fixtures"], strict=True):
        for key in ("sha256", "gold_sha256"):
            if a[key] != b[key]:
                print(f"MISMATCH {a['id']} {key}: committed {a[key]} fresh {b[key]}")
                bad += 1
    print("fixtures reproduce byte-for-byte" if not bad else f"{bad} mismatches")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
