"""Pure benchmark logic shared by the notebook and its CPU-only tests.

build_notebook.py inlines this file verbatim into the notebook's helper cell, so the
tests in tests/ exercise exactly the code that runs on Colab. Standard library only.
"""

import json
import statistics
import time

OK_STATUSES = ("ok",)
FAILED_STATUSES = ("failed", "interrupted")


# ---- streaming ------------------------------------------------------------------
def consume_sse(lines, stop_after_chunks=None, clock=time.perf_counter, t0=None):
    """Read an OpenAI-style chat-completions SSE stream.

    Stops early (the caller then closes the connection) once `stop_after_chunks` content
    chunks have arrived, but only while the stream is still unfinished. The result says
    whether the client really abandoned an unfinished generation: `client_closed_early`
    is True only if we stopped reading before any finish_reason or [DONE] arrived.
    `t0` is the time the request was sent (defaults to now), so TTFT includes the round trip.
    """
    t0 = clock() if t0 is None else t0
    ttft, parts, usage, finish = None, [], {}, None
    chunks, done, stopped = 0, False, False
    for raw in lines:
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            done = True
            break
        obj = json.loads(data)
        if obj.get("usage"):
            usage = obj["usage"]
        for choice in obj.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                if ttft is None:
                    ttft = clock() - t0
                parts.append(piece)
                chunks += 1
            finish = choice.get("finish_reason") or finish
        if stop_after_chunks and chunks >= stop_after_chunks and finish is None:
            stopped = True
            break
    return {
        "text_raw": "".join(parts),
        "ttft_s": ttft,
        "total_s": clock() - t0,
        "usage": usage,
        "finish_reason": finish,
        "chunks": chunks,
        "stream_done": done,
        "client_closed_early": stopped and finish is None and not done,
    }


def cancellation_verdict(stream, running_after, follow_up_ok, server_alive):
    """A cancellation test passes only with evidence that an unfinished stream was abandoned,
    the server released it, and the server still serves the next request."""
    reasons = []
    if not stream.get("client_closed_early"):
        reasons.append(
            "nothing was cancelled: the stream finished before the client closed it "
            f"(finish_reason={stream.get('finish_reason')!r}, chunks={stream.get('chunks')}, "
            f"stream_done={stream.get('stream_done')})"
        )
    if running_after is None:
        reasons.append("vllm:num_requests_running was not reported")
    elif running_after != 0:
        reasons.append(f"cancelled request still running ({running_after})")
    if not follow_up_ok:
        reasons.append("follow-up request after cancellation failed")
    if not server_alive:
        reasons.append("server process died")
    return {"ok": not reasons, "reasons": reasons}


# ---- configuration verdicts ------------------------------------------------------
def required_stages(mode, repeats):
    """Stages a configuration must pass. Target mode includes restart for every configuration."""
    if mode == "small":
        return ["vllm_start", "aux_load", "warmup", "embed_corpus", "document_question"]
    if mode == "target":
        return (["vllm_start", "aux_load", "warmup", "embed_corpus"]
                + [f"questions_rep{r}" for r in range(1, repeats + 1)]
                + ["concurrent", "cancellation", "restart"])
    raise ValueError(f"unknown mode {mode!r}")


def evaluate_config(statuses, mode, repeats, oom_seen=False):
    """Classify one configuration as passed, failed or incomplete.

    `statuses` maps short stage names (without the config prefix) to "ok", "failed",
    "skipped" or "interrupted". A required stage that is missing or skipped makes the
    configuration incomplete; it can never be passed or recommended.
    """
    req = required_stages(mode, repeats)
    failed = [s for s in req if statuses.get(s) in FAILED_STATUSES]
    incomplete = [{"stage": s, "status": statuses.get(s, "not run")}
                  for s in req if statuses.get(s) not in OK_STATUSES + FAILED_STATUSES]
    if failed or oom_seen:
        verdict = "failed"
    elif incomplete:
        verdict = "incomplete"
    else:
        verdict = "passed"
    return {
        "verdict": verdict,
        "required_stages": req,
        "failed_stages": failed,
        "incomplete_stages": incomplete,
        "oom_seen": bool(oom_seen),
        # Only a fully passed target-mode configuration may be recommended.
        "eligible_for_recommendation": verdict == "passed" and mode == "target",
    }


def choose_candidate(config_summaries):
    """Utilization of the eligible configuration with the most measured headroom, or None."""
    eligible = [s for s in config_summaries.values() if s.get("eligible_for_recommendation")]
    if not eligible:
        return None
    return max(eligible, key=lambda s: (s.get("min_headroom_mib") or 0))["utilization"]


# ---- statistics ------------------------------------------------------------------
def stats(values):
    v = sorted(x for x in values if isinstance(x, (int, float)) and not isinstance(x, bool))
    if not v:
        return None
    p90 = v[min(len(v) - 1, int(round(0.9 * (len(v) - 1))))]
    return {"n": len(v), "median": round(statistics.median(v), 3), "p90": round(p90, 3), "max": round(v[-1], 3)}
