"""Partial Colab runs: stage tracking, the 24-pair summary and batch-scoped report generation."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bench import colab_status as cs

ROOT = Path(__file__).resolve().parents[1]
BATCH = "20261121T000000Z-test01"


def write_results(results_dir: Path, run_id: str, batch: str, records: list[dict], where: str = "colab") -> None:
    env = {"where": where, "python": "3.12.12", "cpu_count": 2, "gpu": "Tesla T4, 15360 MiB, 550.54"}
    doc = {"run_id": run_id, "batch": batch, "environment": env, "packages": {}, "results": records}
    (results_dir / f"{run_id}.json").write_text(json.dumps(doc))


def ok(adapter: str, fixture: str) -> dict:
    return {"adapter": adapter, "fixture": fixture, "status": "ok", "metrics": {}}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.status = cs.Status(self.results / BATCH)
        self.pairs = cs.expected_pairs()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def env_pairs(self, env: str) -> list[tuple[str, str]]:
        return [(a, f) for e, a, f in self.pairs if e == env]

    def by_pair(self, summary: dict) -> dict[tuple[str, str], dict]:
        return {(r["adapter"], r["fixture"]): r for r in summary["pairs"]}


class ExpectedPairs(unittest.TestCase):
    def test_twenty_four_pairs_split_by_environment(self) -> None:
        pairs = cs.expected_pairs()
        self.assertEqual(len(pairs), 24)
        self.assertEqual(len(set(pairs)), 24)
        self.assertEqual(sum(1 for e, _, _ in pairs if e == "docling"), 11)  # docling ×7, pymupdf_layout ×4
        self.assertEqual(sum(1 for e, _, _ in pairs if e == "unstructured"), 13)  # 3 PDF strategies ×4, xlsx ×1


class PartialRuns(Base):
    def test_one_environment_fails_to_install_the_other_finishes(self) -> None:
        self.status.record("docling", "install", True, 0)
        self.status.record("docling", "run", True, 0)
        self.status.record("unstructured", "install", False, 1, "No solution found when resolving dependencies")
        self.status.record("unstructured", "run", False, None, "skipped: the install stage failed")
        docling = self.env_pairs("docling")
        records = [ok(a, f) for a, f in docling[1:]]
        records.append(
            {
                "adapter": docling[0][0],
                "fixture": docling[0][1],
                "status": "failed",
                "error": "RuntimeError: layout model download failed",
            }
        )
        write_results(self.results, "run-docling", BATCH, records)
        # A different batch's successes must not count for this batch.
        write_results(self.results, "old-run", "older-batch", [ok(a, f) for a, f in self.env_pairs("unstructured")])

        s = cs.summarize(self.status, self.results, BATCH)
        self.assertEqual(s["expected"], 24)
        self.assertEqual(s["counts"], {"succeeded": 10, "failed": 14, "missing": 0})
        rows = self.by_pair(s)
        self.assertEqual(rows[docling[0]]["stage"], "parse")
        self.assertIn("layout model download failed", rows[docling[0]]["detail"])
        for pair in self.env_pairs("unstructured"):
            self.assertEqual(rows[pair]["state"], "failed")
            self.assertEqual(rows[pair]["stage"], "install")

    def test_runner_crash_keeps_finished_pairs_and_names_the_runner_stage(self) -> None:
        self.status.record("unstructured", "install", True, 0)
        self.status.record("unstructured", "run", False, 137, "Killed")
        done = self.env_pairs("unstructured")[:2]
        write_results(self.results, "run-unstructured", BATCH, [ok(a, f) for a, f in done])
        (self.results / "truncated.json").write_text('{"batch": "' + BATCH)  # never crashes the summary

        s = cs.summarize(self.status, self.results, BATCH)
        rows = self.by_pair(s)
        self.assertEqual(s["counts"], {"succeeded": 2, "failed": 11, "missing": 11})
        for pair in self.env_pairs("unstructured")[2:]:
            self.assertEqual((rows[pair]["state"], rows[pair]["stage"]), ("failed", "runner"))
            self.assertIn("exit code 137", rows[pair]["detail"])
        for pair in self.env_pairs("docling"):  # no stage recorded at all
            self.assertEqual((rows[pair]["state"], rows[pair]["stage"]), ("missing", None))

    def test_summary_markdown_lists_every_pair_and_stage(self) -> None:
        self.status.record("docling", "install", False, 2, "error: resolution failed")
        md = cs.summary_markdown(cs.summarize(self.status, self.results, BATCH))
        self.assertIn("24 expected pairs: 0 succeeded, 11 failed, 13 missing.", md)
        self.assertIn("| docling | install | FAILED | 2 | error: resolution failed |", md)
        self.assertEqual(
            sum(
                1
                for line in md.splitlines()
                if line.startswith(("| docling |", "| unstructured |")) and line.count("|") == 7
            ),
            24,
        )

    def test_status_survives_reopening(self) -> None:
        self.status.record("docling", "install", True, 0)
        again = cs.Status(self.results / BATCH)
        self.assertTrue(again.stage("docling", "install")["ok"])


class ReportGeneration(Base):
    def test_failed_report_shows_no_tables_and_is_recorded(self) -> None:
        stale = self.status.dir / "results-tables.md"
        stale.write_text(f"tables from an earlier attempt, batch `{BATCH}`")
        (self.results / "broken.json").write_text("{not json")  # makes bench.report raise

        self.assertIsNone(cs.run_report(self.status, ROOT, BATCH, self.results))
        self.assertFalse(stale.exists(), "a stale tables file must not survive a failed report")
        rep = self.status.data["report"]
        self.assertFalse(rep["ok"])
        self.assertNotEqual(rep["returncode"], 0)
        self.assertIn("JSONDecodeError", rep["detail"])
        md = cs.summary_markdown(cs.summarize(self.status, self.results, BATCH))
        self.assertIn("| — | report | FAILED |", md)

    def test_successful_report_is_scoped_to_the_batch(self) -> None:
        laptop = next(
            p
            for p in sorted((ROOT / "results").glob("*.json"))
            if json.loads(p.read_text(encoding="utf-8"))["environment"]["where"] == "laptop"
        )
        shutil.copy(laptop, self.results / laptop.name)
        a, f = self.env_pairs("docling")[0]
        write_results(
            self.results,
            "run-docling",
            BATCH,
            [{"adapter": a, "fixture": f, "status": "failed", "error": "RuntimeError: boom"}],
        )
        path = cs.run_report(self.status, ROOT, BATCH, self.results)
        self.assertIsNotNone(path)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"Colab batch `{BATCH}`: 1 result file(s)", text)
        self.assertIn("failed: RuntimeError: boom", text)
        self.assertTrue(self.status.data["report"]["ok"])


class NotebookWiring(unittest.TestCase):
    def setUp(self) -> None:
        nb = json.loads((ROOT / "m1_parser_benchmark.ipynb").read_text(encoding="utf-8"))
        self.cells = ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]

    def test_tables_are_shown_only_after_a_successful_report(self) -> None:
        report_cell = self.cells[4]
        self.assertIn("tables = cs.run_report(STATUS, BENCH, BATCH, env=BENCH_ENV)", report_cell)
        self.assertIn("if tables is not None:", report_cell)
        self.assertNotIn("reports/results-tables.md", report_cell)  # never the committed copy

    def test_environments_are_tracked_separately(self) -> None:
        install, run = self.cells[2], self.cells[3]
        self.assertIn('STATUS.record(name, "install"', install)
        self.assertIn('STATUS.record(name, "run"', run)
        self.assertIn("skipped: the install stage failed", run)

    def test_benchmark_subprocesses_get_the_agg_matplotlib_backend(self) -> None:
        # Colab's kernel exports the inline backend; the venvs' Matplotlib rejects it (batch
        # 20261120T220024Z-907146). Execute cell 1 under that environment and check what a
        # benchmark subprocess actually sees.
        import os
        import subprocess
        from unittest import mock

        inline = {"MPLBACKEND": "module://matplotlib_inline.backend_inline"}
        quiet = contextlib.redirect_stdout(io.StringIO())  # the cell prints progress
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, inline), quiet:
            ns: dict = {}
            with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                exec(self.cells[0], ns)  # cell 1 defines BENCH_ENV and run_logged
            self.assertEqual(ns["BENCH_ENV"]["MPLBACKEND"], "Agg")
            log = Path(tmp) / "probe.log"
            rc = ns["run_logged"]('python3 -c \'import os; print("backend=" + os.environ["MPLBACKEND"])\'', log)
            self.assertEqual(rc, 0)
            self.assertIn("backend=Agg", log.read_text())

    def test_every_benchmark_subprocess_uses_bench_env(self) -> None:
        install, run, report = self.cells[2], self.cells[3], self.cells[4]
        for cell in (install, run):
            self.assertNotIn("subprocess.", cell)  # all commands go through run_logged
            self.assertNotIn("env=", cell)  # so none can override BENCH_ENV
        self.assertIn("bench.run --where colab", run)
        self.assertIn("env=BENCH_ENV", report)

    def test_download_cell_does_not_depend_on_earlier_success(self) -> None:
        download = self.cells[5]
        self.assertIn('globals().get("BENCH")', download)
        self.assertNotIn("STATUS", download)
        self.assertIn('doc.get("batch") == BATCH', download)


if __name__ == "__main__":
    unittest.main()
