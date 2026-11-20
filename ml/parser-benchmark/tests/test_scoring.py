"""Scorer behaviour on small hand-made inputs, and the oracle check on every gold file."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bench.adapters import decode_csv_bytes, html_tables
from bench.scoring import norm_text, score, score_rows, xlsx_equal

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "fixtures/manifest.json").read_text(encoding="utf-8"))


def gold(fid: str) -> dict:
    return json.loads((ROOT / f"fixtures/gold/{fid}.json").read_text(encoding="utf-8"))


def oracle(g: dict) -> dict:
    """A perfect parser: gold text, gold tables, gold headings / rows / cells."""
    if g["case"].endswith("_pdf"):
        tables = [[*t["header_rows"], *t["rows"]] for t in g["tables"]]
        return {"text": g["text"], "tables": tables, "headings": g["headings"]}
    if g["case"] == "csv":
        return {"rows": g["rows"]}
    sheets = []
    for s in g["sheets"]:
        from bench.scoring import _cell_rc

        cells = {_cell_rc(ref): c for ref, c in s["cells"].items()}
        nr = max(r for r, _ in cells) + 1
        nc = max(c for _, c in cells) + 1
        grid = [[str(cells[(r, c)]["value"]) if (r, c) in cells else "" for c in range(nc)] for r in range(nr)]
        fx = {ref: c["formula"] for ref, c in s["cells"].items() if c["formula"]}
        sheets.append({"name": s["name"], "hidden": s["hidden"], "grid": grid, "formulas": fx})
    return {"sheets": sheets}


class OracleScoresPerfect(unittest.TestCase):
    def test_every_fixture(self) -> None:
        for e in MANIFEST["fixtures"]:
            g = gold(e["id"])
            m = score(g, oracle(g))
            with self.subTest(fixture=e["id"]):
                if g["case"].endswith("_pdf"):
                    for k in (
                        "text_similarity",
                        "word_recall",
                        "word_precision",
                        "reading_order",
                        "anchors_found",
                        "heading_recall",
                    ):
                        self.assertEqual(m[k], 1.0, k)
                    if g["tables"]:
                        self.assertEqual(m["table_cells"], 1.0)
                        self.assertEqual(m["table_headers"], 1.0)
                        self.assertEqual(m["table_values_in_text"], 1.0)
                elif g["case"] == "csv":
                    self.assertEqual(m["field_exact"], 1.0)
                    self.assertTrue(all(v == 1.0 for v in m["checks"].values()), m["checks"])
                else:
                    self.assertEqual(m["cell_values"], 1.0)
                    self.assertEqual(m["formulas"], 1.0)
                    self.assertEqual(m["hidden_sheet"], "flagged")


class PdfMetrics(unittest.TestCase):
    def test_markdown_markup_is_not_scored(self) -> None:
        self.assertEqual(
            norm_text("# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n- item **bold**"), "Title a b 1 2 item bold"
        )

    def test_reading_order_penalises_swapped_columns(self) -> None:
        g = gold("two-column-newsletter")
        blocks = g["text_blocks"]
        half = len(blocks) // 2
        swapped = "\n".join(blocks[half:] + blocks[:half])
        m = score(g, {"text": swapped, "tables": None, "headings": None})
        self.assertEqual(m["word_recall"], 1.0)
        self.assertLess(m["reading_order"], 0.7)

    def test_furniture_is_ignored(self) -> None:
        g = gold("digital-company-overview")
        m = score(g, {"text": g["text"] + "\n" + g["furniture"][0], "tables": None, "headings": None})
        self.assertEqual(m["text_similarity"], 1.0)

    def test_table_split_over_pages_is_pooled(self) -> None:
        g = [["a", "1"], ["b", "2"], ["c", "3"]]
        self.assertEqual(score_rows(g, [["a", "1"], ["b", "2"], ["c", "3"]]), (6, 6))

    def test_shifted_columns_lose_cells(self) -> None:
        g = [["NIL-0001", "Workflow", "Enterprise", "129"]]
        merged = [["NIL-0001", "Workflow Enterprise", "129"]]
        self.assertEqual(score_rows(g, merged), (2, 4))

    def test_rows_need_their_key_cell(self) -> None:
        g = [["FULFILLED", "291", "80,934,385.00"]]
        other_table = [["Total", "291", "80,934,385.00", "11,330,813.90"]]
        self.assertEqual(score_rows(g, other_table), (0, 3))

    def test_no_table_output_is_not_applicable(self) -> None:
        g = gold("table-heavy-sales-q1-2026")
        m = score(g, {"text": g["text"], "tables": None, "headings": None})
        self.assertIsNone(m["table_cells"])
        self.assertEqual(m["table_values_in_text"], 1.0)

    def test_spurious_tables_counted(self) -> None:
        g = gold("digital-company-overview")
        m = score(g, {"text": g["text"], "tables": [[["x", "y"]]], "headings": None})
        self.assertEqual(m["spurious_tables"], 1)


class SpreadsheetMetrics(unittest.TestCase):
    def test_xlsx_equivalences(self) -> None:
        num = {"value": "80934385.00", "display": "80,934,385.00", "type": "num"}
        self.assertTrue(xlsx_equal(num, "80934385.0"))
        self.assertTrue(xlsx_equal(num, "80,934,385.00"))
        self.assertFalse(xlsx_equal(num, "80934386"))
        date = {"value": "2026-01-01", "display": "2026-01-01", "type": "date"}
        self.assertTrue(xlsx_equal(date, "2026-01-01 00:00:00"))
        self.assertTrue(xlsx_equal(date, "2026-01-01T00:00:00"))
        pct = {"value": "0.3299357812", "display": "33.0%", "type": "num"}
        self.assertTrue(xlsx_equal(pct, "33.0%"))
        text_num = {"value": "00123", "display": "00123", "type": "str"}
        self.assertFalse(xlsx_equal(text_num, "123"))

    def test_csv_decoding(self) -> None:
        self.assertEqual(decode_csv_bytes("€".encode("cp1252")), ("€", "cp1252"))
        self.assertEqual(decode_csv_bytes(b"\xef\xbb\xbfa"), ("a", "utf-8-sig"))

    def test_html_tables_expand_colspan(self) -> None:
        html = "<table><tr><th colspan='2'>Q1</th></tr><tr><td>a</td><td>b<br/>c</td></tr></table>"
        self.assertEqual(html_tables(html), [[["Q1", ""], ["a", "b\nc"]]])


if __name__ == "__main__":
    unittest.main()
