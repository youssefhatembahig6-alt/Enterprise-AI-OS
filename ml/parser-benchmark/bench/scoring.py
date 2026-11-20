"""Score parser output against the gold references in ``fixtures/gold``.

Metrics (all in [0, 1], higher is better; ``None`` means "not applicable to this output"):

PDF
  text_similarity    order-sensitive: normalized Levenshtein similarity of the whole text
  word_recall        completeness: share of gold words present (multiset)
  word_precision     share of extracted words that are in the gold text (multiset)
  reading_order      longest in-order run of gold anchors found / number of anchors
  anchors_found      anchors found anywhere / number of anchors
  table_cells        body cells recovered in the right row and column order / gold body cells
  table_headers      header cells recovered / gold header cells (last header row)
  table_values_in_text  gold body cell strings that occur anywhere in the text
  heading_recall     gold headings the parser labelled as headings (typed output only)
  spurious_tables    tables reported on a fixture that has none (a count; lower is better)

CSV
  field_exact        gold fields reproduced byte-for-byte at the same row and column
  field_normalized   same, after whitespace collapse (separates quoting from spacing errors)
  rows_match         1.0 if the row count matches, else 0.0
  checks             named semantic checks (encoding, leading zeros, embedded newline, ...)

XLSX
  cell_values        visible-sheet gold cells recovered (value or displayed form) / gold cells
  sheets_found       visible gold sheets present (None when the parser names no sheets; its
                     unnamed grids are then pooled and matched to gold rows by content)
  hidden_sheet       "flagged" | "unflagged" | "unknown" | "skipped" (see hidden_sheet_state)
  formulas           gold formulas recovered exactly (None if the parser exposes none)
  merged_title       merge range and top-left title preserved in the expected sheet
                     (None if the parser exposes no merge metadata)
  title_text         the merged title's text is present in the expected sheet
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

_TABLE_RULE = re.compile(r"^[\s|:\-+=]+$")
_LEAD_MARK = re.compile(r"^(#{1,6}\s+|[•\-\*·▪◦●]\s+)")
_WS = re.compile(r"\s+")
_EDGE_PUNCT = "\"'“”‘’()[]{}<>.,;:!?"


def norm_text(text: str) -> str:
    """Normalise extracted or gold text so that markup style is not scored.

    NFKC, drop soft hyphens, drop Markdown table rules, heading marks, bullets, pipes and
    bold/italic markers, then collapse all whitespace to single spaces.
    """
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _TABLE_RULE.match(line):
            continue
        line = _LEAD_MARK.sub("", line)
        line = line.replace("|", " ").replace("**", "").replace("__", "")
        out.append(line)
    return _WS.sub(" ", " ".join(out)).strip()


def norm_cell(value: Any) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", str(value)).replace("**", "")).strip()


def words(text: str) -> list[str]:
    return [w.strip(_EDGE_PUNCT) for w in text.split() if w.strip(_EDGE_PUNCT)]


def strip_furniture(text_norm: str, furniture: Iterable[str]) -> str:
    for f in furniture:
        fn = norm_text(f)
        if fn:
            text_norm = text_norm.replace(fn, " ")
    return _WS.sub(" ", text_norm).strip()


def find_anchor(hay: str, anchor: str, start: int = 0) -> int:
    """Position of ``anchor`` in ``hay`` (both normalised); exact first, then fuzzy (>= 90)."""
    a = norm_text(anchor)
    pos = hay.find(a, start)
    if pos >= 0:
        return pos
    pos = hay.find(a)
    if pos >= 0:
        return pos
    if len(a) < 8:
        return -1
    al = fuzz.partial_ratio_alignment(a, hay, score_cutoff=90)
    return al.dest_start if al is not None else -1


def longest_increasing(seq: Sequence[int]) -> int:
    import bisect

    tails: list[int] = []
    for x in seq:
        i = bisect.bisect_left(tails, x)
        if i == len(tails):
            tails.append(x)
        else:
            tails[i] = x
    return len(tails)


def _lcs_matches(a: Sequence[str], b: Sequence[str], eq: Callable[[str, str], bool]) -> int:
    """Length of the longest common subsequence of two short cell lists."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if eq(x, y) else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def _eq_cell(a: str, b: str) -> bool:
    return a == b


def score_rows(
    gold_rows: list[list[str]], ext_rows: list[list[str]], eq: Callable[[str, str], bool] = _eq_cell
) -> tuple[int, int]:
    """Best-match each gold row to an extracted row; count cells in the right order.

    Extracted rows are pooled across tables (a table split over a page break comes back as two
    tables). A row is only matched to extracted rows that contain its key (first) cell. Empty extracted cells are dropped before matching, so spanning cells and blank
    padding columns are not penalised; merged or split cells are.
    """
    ext = [[norm_cell(c) for c in r if norm_cell(c)] for r in ext_rows]
    ext = [r for r in ext if r]
    index: dict[str, list[int]] = {}
    for i, r in enumerate(ext):
        for c in set(r):
            index.setdefault(c, []).append(i)
    total = matched = 0
    for g in gold_rows:
        gn = [norm_cell(c) for c in g if norm_cell(c)]
        total += len(gn)
        # Row identity: an extracted row counts only if it contains the gold row's key (first)
        # cell, so a different table's row that shares a total is not credited.
        cands = set(index.get(gn[0], ())) if gn else set()
        best = max((_lcs_matches(gn, ext[i], eq) for i in cands), default=0)
        matched += best
    return matched, total


# --------------------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------------------


def score_pdf(gold: dict, out: dict) -> dict:
    gold_text = norm_text(gold["text"])
    ext_text = strip_furniture(norm_text(out.get("text") or ""), gold.get("furniture", []))
    m: dict[str, Any] = {"chars_extracted": len(ext_text), "chars_gold": len(gold_text)}
    m["text_similarity"] = round(Levenshtein.normalized_similarity(ext_text, gold_text), 4)
    gw, ew = Counter(words(gold_text)), Counter(words(ext_text))
    inter = sum((gw & ew).values())
    m["word_recall"] = round(inter / max(1, sum(gw.values())), 4)
    m["word_precision"] = round(inter / sum(ew.values()), 4) if ew else 0.0

    positions, cursor = [], 0
    for a in gold["reading_order"]:
        p = find_anchor(ext_text, a, cursor)
        positions.append(p)
        if p >= 0:
            cursor = p
    found = [p for p in positions if p >= 0]
    n = len(gold["reading_order"])
    m["anchors_found"] = round(len(found) / n, 4)
    m["reading_order"] = round(longest_increasing(found) / n, 4)

    tables = out.get("tables")
    if gold["tables"]:
        body = [r for t in gold["tables"] for r in t["rows"]]
        heads = [t["header_rows"][-1] for t in gold["tables"]]
        if tables is None:
            m["table_cells"] = None
            m["table_headers"] = None
        else:
            pooled = [row for t in tables for row in t]
            mb, tb = score_rows(body, pooled)
            mh, th = score_rows(heads, pooled)
            m["table_cells"] = round(mb / tb, 4)
            m["table_headers"] = round(mh / th, 4)
            m["table_cells_by_table"] = {
                t["id"]: round(score_rows(t["rows"], pooled)[0] / sum(len(r) for r in t["rows"]), 4)
                for t in gold["tables"]
            }
        m["tables_returned"] = None if tables is None else len(tables)
        m["tables_gold"] = len(gold["tables"])
        cells = [norm_cell(c) for r in body for c in r]
        m["table_values_in_text"] = round(sum(1 for c in cells if c in ext_text) / len(cells), 4)
    else:
        # Tables reported on a page that has none (prose split into a grid).
        m["spurious_tables"] = None if tables is None else len(tables)
    heads_out = out.get("headings")
    if heads_out is None:
        m["heading_recall"] = None
    else:
        hn = [norm_text(h) for h in heads_out]
        hit = sum(1 for h in gold.get("headings", []) if any(fuzz.ratio(norm_text(h), x) >= 90 for x in hn))
        m["heading_recall"] = round(hit / max(1, len(gold.get("headings", []))), 4)
    return m


# --------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------


def score_csv(gold: dict, out: dict) -> dict:
    g = gold["rows"]
    e = [[("" if c is None else str(c)) for c in r] for r in (out.get("rows") or [])]
    total = sum(len(r) for r in g)
    exact = normed = 0
    for i, row in enumerate(g):
        er = e[i] if i < len(e) else []
        for j, cell in enumerate(row):
            ec = er[j] if j < len(er) else None
            if ec is None:
                continue
            exact += ec == cell
            normed += norm_cell(ec) == norm_cell(cell)
    flat = {c for r in e for c in r}
    ck = gold["checks"]
    checks = {
        "non_ascii_exact": sum(1 for s in ck["non_ascii"] if s in flat) / len(ck["non_ascii"]),
    }
    if "leading_zero_codes" in ck:
        checks["leading_zeros"] = sum(1 for s in ck["leading_zero_codes"] if s in flat) / len(ck["leading_zero_codes"])
        checks["embedded_newline"] = float(ck["embedded_newline"] in flat)
        checks["doubled_quotes"] = float(ck["doubled_quotes"] in flat)
        checks["formula_text_kept"] = float(ck["formula_text"] in flat)
    if "quoted_delimiter" in ck:
        checks["quoted_delimiter"] = float(ck["quoted_delimiter"] in flat)
        checks["decimal_comma_text"] = sum(1 for s in ck["decimal_comma"] if s in flat) / len(ck["decimal_comma"])
    return {
        "field_exact": round(exact / total, 4),
        "field_normalized": round(normed / total, 4),
        "rows_match": float(len(e) == len(g)),
        "rows_extracted": len(e),
        "rows_gold": len(g),
        "checks": {k: round(v, 4) for k, v in checks.items()},
    }


# --------------------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------------------

_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def xlsx_equal(gold_cell: dict, ext: str) -> bool:
    ext = norm_cell(ext)
    if not ext:
        return False
    if ext in (norm_cell(gold_cell["value"]), norm_cell(gold_cell["display"])):
        return True
    typ = gold_cell["type"]
    if typ == "num":
        a, b = _num(ext.rstrip("%")), _num(str(gold_cell["value"]))
        if a is not None and b is not None:
            if ext.endswith("%"):
                a /= 100
                return abs(a - b) <= 0.0005  # displayed to 0.1 %
            return abs(a - b) <= 1e-6  # float round-trip of 2-dp money and 10-dp shares
    if typ == "date":
        mt = _ISO.match(ext)
        return bool(mt and mt.group(1) == gold_cell["value"])
    if typ == "bool":
        return ext.lower() in ("true", "1")
    return False


def _cell_rc(ref: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    assert m
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)) - 1, col - 1


def score_xlsx(gold: dict, out: dict) -> dict:
    sheets_out = out.get("sheets") or []
    by_name = {s.get("name"): s for s in sheets_out}
    visible = [s for s in gold["sheets"] if not s["hidden"]]
    matched = total = found = 0
    per_sheet = {}
    formulas_total = formulas_hit = 0
    formulas_available = any(s.get("formulas") is not None for s in sheets_out)
    named_output = any(s.get("name") for s in sheets_out)
    # A parser that names no sheets may also split or merge them (Docling returns one table
    # per detected table, e.g. a title and its table separately). Position therefore says
    # nothing about which sheet a grid came from: pool all unnamed grids and match rows by
    # content. Sheet identification is then not measurable (sheets_found = None).
    pooled = None
    if sheets_out and not named_output:
        pooled = {
            "name": None,
            "hidden": None,
            "grid": [row for sh in sheets_out for row in (sh.get("grid") or [])],
            "formulas": None,
            "merged": None if all(sh.get("merged") is None for sh in sheets_out) else [],
        }
    matched_sheet: dict[str, dict | None] = {}
    for gs in gold["sheets"]:
        es = pooled if pooled is not None else by_name.get(gs["name"])
        matched_sheet[gs["name"]] = es
        rows: dict[int, list[tuple[int, dict]]] = {}
        for ref, cell in gs["cells"].items():
            r, c = _cell_rc(ref)
            rows.setdefault(r, []).append((c, cell))
        gold_rows = [[cell for _, cell in sorted(v, key=lambda t: t[0])] for _, v in sorted(rows.items())]
        n_cells = sum(len(r) for r in gold_rows)
        if not gs["hidden"]:
            total += n_cells
        if es is None:
            per_sheet[gs["name"]] = 0.0
            continue
        if not gs["hidden"]:
            found += 1
        grid = es.get("grid") or []
        ext_rows = [[norm_cell(c) for c in r if norm_cell(c)] for r in grid]
        ext_rows = [r for r in ext_rows if r]
        index: dict[str, set[int]] = {}
        for k, er in enumerate(ext_rows):
            for c in er:
                index.setdefault(c, set()).add(k)
        hit = 0
        for g in gold_rows:
            cands: set[int] = set()
            for cell in g:
                for key in (norm_cell(cell["value"]), norm_cell(cell["display"])):
                    cands |= index.get(key, set())
            pool = cands if cands else range(len(ext_rows))  # e.g. numbers rendered differently
            best = 0
            for k in pool:
                best = max(best, _lcs_cells(g, ext_rows[k]))
                if best == len(g):
                    break
            hit += best
        per_sheet[gs["name"]] = round(hit / n_cells, 4)
        if not gs["hidden"]:
            matched += hit
        fx = es.get("formulas")
        for ref, cell in gs["cells"].items():
            if cell.get("formula"):
                formulas_total += 1 if not gs["hidden"] else 0
                if fx and fx.get(ref) == cell["formula"]:
                    formulas_hit += 1
    hidden_gold = [s for s in gold["sheets"] if s["hidden"]]
    hidden_state = "n/a"
    if hidden_gold:
        hidden_state = hidden_sheet_state(hidden_gold[0]["name"], sheets_out, by_name, named_output)
    merge = score_merged_title(gold, matched_sheet)
    return {
        "cell_values": round(matched / total, 4),
        "sheets_found": None if pooled is not None else round(found / len(visible), 4),
        "per_sheet": per_sheet,
        "hidden_sheet": hidden_state,
        "formulas": round(formulas_hit / formulas_total, 4) if formulas_available else None,
        **merge,
    }


def hidden_sheet_state(name: str, sheets_out: list[dict], by_name: dict, named_output: bool) -> str:
    """flagged | unflagged | unknown | skipped for the gold hidden sheet ``name``.

    ``unknown`` covers two cases where the output cannot show whether the hidden sheet was kept
    and marked: the sheet is present but the parser exposes no visibility (``hidden is None``),
    or the parser names no sheets, so any unnamed output sheet could be the hidden one.
    ``skipped`` is used only when the output names its sheets and the hidden one is absent.
    """
    es = by_name.get(name)
    if es is not None:
        if es.get("hidden") is True:
            return "flagged"
        if es.get("hidden") is False:
            return "unflagged"
        return "unknown"
    if any(not s.get("name") for s in sheets_out):
        return "unknown"
    return "skipped"


def score_merged_title(gold: dict, matched_sheet: dict[str, dict | None]) -> dict:
    """Merged-title checks against the expected sheet and merge range.

    merged_title  1.0 only if the expected output sheet reports the exact merged range and holds
                  the title at that range's top-left cell; 0.0 if the parser exposes merge metadata
                  but the range or the text is wrong, or the sheet is missing; ``None`` when the
                  parser exposes no merge metadata for that sheet (unavailable, not a failure).
    title_text    1.0 if the title text is anywhere in the expected sheet (plain text is credited
                  here and never as a merge).
    """
    merged_hits = merged_total = text_hits = 0
    unavailable = False
    for gs in gold["sheets"]:
        for rng in gs["merged"]:
            top_left = rng.split(":")[0]
            title = norm_cell(gs["cells"][top_left]["value"])
            merged_total += 1
            es = matched_sheet.get(gs["name"])
            if es is None:
                continue
            grid = es.get("grid") or []
            if any(norm_cell(c) == title for r in grid for c in r):
                text_hits += 1
            ranges = es.get("merged")
            if ranges is None:
                unavailable = True
                continue
            r, c = _cell_rc(top_left)
            at_top_left = norm_cell(grid[r][c]) if r < len(grid) and c < len(grid[r]) else ""
            norm_ranges = {x.replace("$", "").upper() for x in ranges}
            if rng.upper() in norm_ranges and at_top_left == title:
                merged_hits += 1
    if not merged_total:
        return {"merged_title": None, "title_text": None}
    return {
        "merged_title": None if unavailable else round(merged_hits / merged_total, 4),
        "title_text": round(text_hits / merged_total, 4),
    }


def _lcs_cells(gold_row: list[dict], ext_row: list[str]) -> int:
    if not gold_row or not ext_row:
        return 0
    prev = [0] * (len(ext_row) + 1)
    for g in gold_row:
        cur = [0]
        for j, e in enumerate(ext_row, 1):
            cur.append(prev[j - 1] + 1 if xlsx_equal(g, e) else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def score(gold: dict, out: dict) -> dict:
    case = gold["case"]
    if case.endswith("_pdf"):
        return score_pdf(gold, out)
    if case == "csv":
        return score_csv(gold, out)
    if case == "xlsx":
        return score_xlsx(gold, out)
    raise ValueError(case)
