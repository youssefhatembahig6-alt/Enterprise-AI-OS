"""CPU-only regression tests for the benchmark's pure logic (standard library only).

Run: python -m unittest discover -s ml/gpu-feasibility/tests -v
"""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bench_core as core  # noqa: E402


def sse(obj):
    return "data: " + json.dumps(obj)


def content(piece):
    return sse({"choices": [{"delta": {"content": piece}, "finish_reason": None}]})


def finish(reason="stop"):
    return sse({"choices": [{"delta": {}, "finish_reason": reason}]})


def usage(prompt=50, completion=5):
    return sse({"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}})


class CountingStream:
    """Mocked server stream that records how many lines the client actually read."""

    def __init__(self, lines):
        self.lines, self.read = list(lines), 0

    def __iter__(self):
        for line in self.lines:
            self.read += 1
            yield line


def fake_clock():
    t = [0.0]

    def clock():
        t[0] += 0.01
        return t[0]

    return clock


# ---- finding 2: cancellation must prove an unfinished stream was abandoned ----------
class StreamCancellationTests(unittest.TestCase):
    def test_natural_early_completion_is_not_a_cancellation(self):
        # The model finishes after 5 chunks, before the client's 20-chunk cut-off.
        stream = CountingStream([content(f"w{i} ") for i in range(5)] + [finish(), usage(), "data: [DONE]"])
        res = core.consume_sse(stream, stop_after_chunks=20, clock=fake_clock())
        self.assertEqual(res["chunks"], 5)
        self.assertEqual(res["finish_reason"], "stop")
        self.assertTrue(res["stream_done"])
        self.assertFalse(res["client_closed_early"])
        # Even with an idle server and a working follow-up, the test must fail.
        verdict = core.cancellation_verdict(res, running_after=0, follow_up_ok=True, server_alive=True)
        self.assertFalse(verdict["ok"])
        self.assertIn("nothing was cancelled", verdict["reasons"][0])

    def test_client_interruption_of_unfinished_stream(self):
        stream = CountingStream([content(f"w{i} ") for i in range(500)] + [finish("length"), "data: [DONE]"])
        res = core.consume_sse(stream, stop_after_chunks=20, clock=fake_clock())
        self.assertEqual(res["chunks"], 20)
        self.assertIsNone(res["finish_reason"])
        self.assertFalse(res["stream_done"])
        self.assertTrue(res["client_closed_early"])
        self.assertEqual(stream.read, 20, "client must stop reading at the cut-off")
        verdict = core.cancellation_verdict(res, running_after=0, follow_up_ok=True, server_alive=True)
        self.assertEqual(verdict, {"ok": True, "reasons": []})

    def test_interruption_without_server_release_fails(self):
        res = core.consume_sse([content("x")] * 50, stop_after_chunks=20, clock=fake_clock())
        self.assertTrue(res["client_closed_early"])
        for running, follow_up, alive, expected in [
            (1.0, True, True, "still running"),
            (None, True, True, "not reported"),
            (0.0, False, True, "follow-up"),
            (0.0, True, False, "died"),
        ]:
            verdict = core.cancellation_verdict(res, running, follow_up, alive)
            self.assertFalse(verdict["ok"])
            self.assertTrue(any(expected in r for r in verdict["reasons"]), verdict)

    def test_finish_on_the_cutoff_chunk_is_not_a_cancellation(self):
        lines = [content(f"w{i}") for i in range(19)]
        lines.append(sse({"choices": [{"delta": {"content": "end"}, "finish_reason": "stop"}]}))
        lines += [usage(), "data: [DONE]"]
        res = core.consume_sse(lines, stop_after_chunks=20, clock=fake_clock())
        self.assertEqual(res["chunks"], 20)
        self.assertFalse(res["client_closed_early"])

    def test_dropped_connection_is_not_a_client_cancellation(self):
        res = core.consume_sse([content("a"), content("b")], stop_after_chunks=20, clock=fake_clock())
        self.assertFalse(res["stream_done"])
        self.assertFalse(res["client_closed_early"])

    def test_normal_stream_reads_text_usage_and_ttft(self):
        lines = [": keep-alive", "", content("Hello"), content(" world"), finish(), usage(12, 2), "data: [DONE]"]
        res = core.consume_sse(lines, clock=fake_clock(), t0=0.0)
        self.assertEqual(res["text_raw"], "Hello world")
        self.assertEqual(res["usage"]["completion_tokens"], 2)
        self.assertGreater(res["ttft_s"], 0)
        self.assertGreaterEqual(res["total_s"], res["ttft_s"])
        self.assertFalse(res["client_closed_early"])


# ---- finding 1: restart is required for any recommended configuration ---------------
def target_statuses(**overrides):
    statuses = {s: "ok" for s in core.required_stages("target", 2)}
    statuses["teardown"] = "ok"
    statuses.update(overrides)
    return {k: v for k, v in statuses.items() if v is not None}


class ConfigVerdictTests(unittest.TestCase):
    def test_target_requires_restart_for_every_configuration(self):
        self.assertIn("restart", core.required_stages("target", 2))
        self.assertIn("cancellation", core.required_stages("target", 2))
        self.assertEqual(core.required_stages("target", 3)[4:7], ["questions_rep1", "questions_rep2", "questions_rep3"])

    def test_all_stages_ok_passes_and_is_eligible(self):
        ev = core.evaluate_config(target_statuses(), "target", 2)
        self.assertEqual(ev["verdict"], "passed")
        self.assertTrue(ev["eligible_for_recommendation"])

    def test_failed_restart_fails_and_is_not_eligible(self):
        ev = core.evaluate_config(target_statuses(restart="failed"), "target", 2)
        self.assertEqual(ev["verdict"], "failed")
        self.assertEqual(ev["failed_stages"], ["restart"])
        self.assertFalse(ev["eligible_for_recommendation"])

    def test_untested_restart_is_incomplete_and_not_eligible(self):
        ev = core.evaluate_config(target_statuses(restart=None), "target", 2)
        self.assertEqual(ev["verdict"], "incomplete")
        self.assertEqual(ev["incomplete_stages"], [{"stage": "restart", "status": "not run"}])
        self.assertFalse(ev["eligible_for_recommendation"])

    def test_skipped_restart_is_incomplete(self):
        ev = core.evaluate_config(target_statuses(restart="skipped"), "target", 2)
        self.assertEqual(ev["verdict"], "incomplete")
        self.assertEqual(ev["incomplete_stages"], [{"stage": "restart", "status": "skipped"}])

    def test_interrupted_stage_or_oom_fails(self):
        self.assertEqual(core.evaluate_config(target_statuses(concurrent="interrupted"), "target", 2)["verdict"], "failed")
        ev = core.evaluate_config(target_statuses(), "target", 2, oom_seen=True)
        self.assertEqual(ev["verdict"], "failed")
        self.assertFalse(ev["eligible_for_recommendation"])

    def test_small_mode_passes_but_is_never_recommended(self):
        statuses = {s: "ok" for s in core.required_stages("small", 2)}
        ev = core.evaluate_config(statuses, "small", 2)
        self.assertEqual(ev["verdict"], "passed")
        self.assertFalse(ev["eligible_for_recommendation"])
        self.assertNotIn("restart", ev["required_stages"])

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            core.required_stages("tiny", 1)


class CandidateTests(unittest.TestCase):
    def summary(self, util, headroom, statuses, oom=False):
        return {"utilization": util, "min_headroom_mib": headroom,
                **core.evaluate_config(statuses, "target", 2, oom_seen=oom)}

    def test_config_with_failed_restart_cannot_win_on_headroom(self):
        configs = {
            "target/u0.85": self.summary(0.85, 900, target_statuses()),
            "target/u0.75": self.summary(0.75, 2400, target_statuses(restart="failed")),
        }
        self.assertEqual(core.choose_candidate(configs), 0.85)

    def test_config_with_untested_restart_cannot_win_on_headroom(self):
        configs = {
            "target/u0.85": self.summary(0.85, 900, target_statuses()),
            "target/u0.75": self.summary(0.75, 2400, target_statuses(restart=None)),
        }
        self.assertEqual(core.choose_candidate(configs), 0.85)

    def test_no_candidate_when_nothing_passed(self):
        configs = {
            "target/u0.85": self.summary(0.85, 900, target_statuses(restart=None)),
            "target/u0.75": self.summary(0.75, 2400, target_statuses(restart="failed")),
        }
        self.assertIsNone(core.choose_candidate(configs))

    def test_most_headroom_among_passed(self):
        configs = {
            "target/u0.85": self.summary(0.85, 900, target_statuses()),
            "target/u0.75": self.summary(0.75, 2400, target_statuses()),
        }
        self.assertEqual(core.choose_candidate(configs), 0.75)


class StatsTests(unittest.TestCase):
    def test_stats(self):
        self.assertIsNone(core.stats([None, "x", True]))
        self.assertEqual(core.stats([3, 1, 2, None]), {"n": 3, "median": 2, "p90": 3, "max": 3})


if __name__ == "__main__":
    unittest.main()
