"""Pure benchmark logic shared by the notebook and its CPU-only tests.

build_notebook.py inlines this file verbatim into the notebook's helper cell, so the
tests in tests/ exercise exactly the code that runs on Colab. Standard library only.
"""

import json
import re
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
    An error event inside the stream (vLLM sends one when its engine dies mid-request) is
    returned in `error`, so the caller can fail instead of counting an empty answer.
    `t0` is the time the request was sent (defaults to now), so TTFT includes the round trip.
    """
    t0 = clock() if t0 is None else t0
    ttft, parts, usage, finish = None, [], {}, None
    chunks, done, stopped, error = 0, False, False, None
    for raw in lines:
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            done = True
            break
        obj = json.loads(data)
        err = obj.get("error") or (obj if obj.get("object") == "error" else None)
        if err:
            error = err if isinstance(err, dict) else {"message": str(err)}
            continue
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
        "error": error,
    }


def stream_failure(res):
    """Why a consumed stream is not a valid answer, or None. A stream the client closed on
    purpose (client_closed_early) is valid; an error event or an unfinished end is not."""
    if res.get("error"):
        return "error event in stream: " + str(res["error"].get("message", res["error"]))[:500]
    if res.get("finish_reason") is None and not res.get("client_closed_early"):
        return f"stream ended without a finish_reason (chunks={res.get('chunks')}, done={res.get('stream_done')})"
    return None


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


# ---- isolated vLLM environment ------------------------------------------------
def parse_pins(text):
    """Parse "name==version" lines (comments and blanks ignored) into {name: version}."""
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        if sep != "==" or not name.strip() or not version.strip():
            raise ValueError(f"pin must be name==version: {line!r}")
        pins[name.strip().lower()] = version.strip()
    return pins


def plan_vllm_env(installed, pins):
    """Decide what to do with the vLLM venv.

    `installed` is None when the venv has no Python, otherwise {package: version or None}.
    An existing venv is reused only when every pinned package matches exactly; any mismatch
    (for example vllm 0.11.0 with transformers 5.23.0 from an earlier unpinned install) is
    repaired in place by reinstalling the pins.
    """
    if installed is None:
        return {"action": "create", "mismatched": {}}
    mismatched = {name: {"installed": installed.get(name), "required": version}
                  for name, version in pins.items() if installed.get(name) != version}
    return {"action": "repair" if mismatched else "reuse", "mismatched": mismatched}


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


# ---- OOM evidence ----------------------------------------------------------------
OOM_PATTERN = re.compile(r"out of memory|OutOfMemoryError", re.I)


def oom_lines(text, limit=3):
    """The first `limit` log lines that report a CUDA/torch out-of-memory error."""
    return [line.strip()[:400] for line in text.splitlines() if OOM_PATTERN.search(line)][:limit]


def config_oom(key, failures, server_logs):
    """Whether configuration `key` hit an OOM, with the evidence.

    `server_logs` maps each vLLM log of this configuration (the original server and every
    restart) to its facts. Every log counts: an OOM in the original server stays recorded
    even when a later restart comes up cleanly.
    """
    evidence = [{"source": f"failure {f['stage']}", "line": f["message"][:400]}
                for f in failures if f.get("kind") == "oom" and f["stage"].startswith(key + "/")]
    for log, facts in server_logs.items():
        lines = facts.get("oom_lines") or (["(oom_in_log)"] if facts.get("oom_in_log") else [])
        evidence += [{"source": log, "line": line} for line in lines]
    return {"oom_seen": bool(evidence), "evidence": evidence}


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
