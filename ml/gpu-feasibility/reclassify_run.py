"""Re-derive configuration verdicts from a saved results ZIP with the current bench_core rules.

Reads results.json and every vLLM log in the ZIP (the original server and each restart), so an
OOM in the original server is counted even when a later restart came up cleanly.

    python ml/gpu-feasibility/reclassify_run.py eaios-m1-gpu-target-<run id>.zip
"""

import json
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bench_core import choose_candidate, config_oom, evaluate_config, oom_lines  # noqa: E402


def reclassify(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        results = json.loads(z.read(next(n for n in names if n.endswith("/results.json") or n == "results.json")))
        logs = {pathlib.PurePosixPath(n).name: z.read(n).decode("utf-8", "replace")
                for n in names if pathlib.PurePosixPath(n).name.startswith("vllm-") and n.endswith(".log")}
    mode, repeats = results["mode"], results["config"]["repeats"]
    out = {}
    for key, cfg in results["configs"].items():
        prefix = "vllm-" + key.replace("/", "-") + "-"
        server_logs = {name: {"oom_lines": oom_lines(text)} for name, text in sorted(logs.items()) if name.startswith(prefix)}
        statuses = {s["name"][len(key) + 1:]: s.get("status") for s in cfg["stages"]}
        oom = config_oom(key, results["failures"], server_logs)
        ev = evaluate_config(statuses, cfg.get("mode", mode), repeats, oom_seen=oom["oom_seen"])
        out[key] = {"utilization": cfg["utilization"], "logs": sorted(server_logs),
                    "min_headroom_mib": (cfg.get("memory_overall") or {}).get("min_headroom_mib"),
                    **ev, "oom_evidence": oom["evidence"]}
    return {"run_id": results["run_id"], "mode": mode, "configs": out,
            "candidate": choose_candidate(out) if mode == "target" else None}


if __name__ == "__main__":
    res = reclassify(sys.argv[1])
    for key, c in res["configs"].items():
        print(f"{key}: {c['verdict']}; failed stages {c['failed_stages'] or 'none'}; "
              f"OOM {'yes' if c['oom_seen'] else 'no'} ({', '.join(sorted({e['source'] for e in c['oom_evidence']})) or 'no evidence'}); "
              f"logs checked: {', '.join(c['logs'])}")
    print(f"candidate: {res['candidate']}")
