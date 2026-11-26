"""M7 functional acceptance: one real-model smoke test in a Colab T4 runtime, loopback only.

Flow under test: verified sales report (real Qwen3-8B-AWQ through the gateway) -> email
draft -> human approval -> durable outbox -> exactly one message in a local Mailpit.

Reuses the M3/M5 Colab setup (``ml/colab-m3/run_m3_colab.py``: PostgreSQL 17, Redis,
Qdrant, the hash-locked backend venv, migrate, seed-dev) and adds what M6 needs:

  business   ``app.cli import-business-data`` (NileTech niletech-0.1.6, hash-verified)
  mailpit    Mailpit v1.27.11 (same version as compose.yaml), SMTP 127.0.0.1:1025,
             API 127.0.0.1:8025; nothing leaves the runtime
  api        uvicorn on 127.0.0.1:18000, MODEL_MODE=gateway, REPORT_DISPATCH=inline
  worker     Celery worker + beat for outbox recovery and reconciliation. With
             REPORT_DISPATCH=inline the API itself delivers through the durable outbox
             (app/main.py); in Compose the worker does
  demo       ``scripts/m6_demo.py`` (the M6 runner, unchanged)
  checks     independent checks on the demo output: every report event says
             ``inference_mode == "gateway"``, run published, and Mailpit holds exactly one
             message in total (a fresh Mailpit database)
  bundle     /content/m7-smoke-evidence-<stamp>.zip after a secret scan

    python run_m7_smoke_colab.py vllm-env   # once per runtime (M1 pinned vLLM venv)
    # launch the gateway (notebook cell 3)
    python run_m7_smoke_colab.py all
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("M3_RUN_DIR", "/content/m7-run")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "colab-m3"))
import run_m3_colab as m3  # noqa: E402

MAILPIT_VERSION = "v1.27.11"
MAILPIT = "http://127.0.0.1:8025"
STEPS: dict[str, object] = {}


def env(**extra: str) -> dict[str, str]:
    return m3.app_env(
        SMTP_HOST="127.0.0.1",
        SMTP_PORT="1025",
        MAILPIT_API_URL=MAILPIT,
        **extra,
    )


def mailpit() -> None:
    binary = Path("/content/mailpit")
    if not binary.exists():
        m3.sh(
            "cd /content && wget -q https://github.com/axllent/mailpit/releases/download/"
            f"{MAILPIT_VERSION}/mailpit-linux-amd64.tar.gz"
            " && tar xzf mailpit-linux-amd64.tar.gz mailpit",
            "mailpit.log",
        )
    db = m3.RUN / "mailpit.db"
    db.unlink(missing_ok=True)  # fresh inbox: the total count is part of the check
    subprocess.Popen(  # noqa: S603
        [
            str(binary),
            "--smtp",
            "127.0.0.1:1025",
            "--listen",
            "127.0.0.1:8025",
            "--database",
            str(db),
        ],
        stdout=open(m3.LOGS / "mailpit.log", "a"),  # noqa: SIM115 - child process owns the log
        stderr=subprocess.STDOUT,
    )
    m3.wait_http(f"{MAILPIT}/readyz")


def celery(kind: str) -> None:
    args = {
        "worker": ["worker", "--concurrency=2", "--loglevel=INFO", "-Q", "default"],
        "beat": ["beat", "--loglevel=INFO", "--schedule", str(m3.RUN / "celerybeat-schedule")],
    }[kind]
    subprocess.Popen(  # noqa: S603
        [f"{m3.VENV}/bin/celery", "-A", "app.tasks.worker:celery_app", *args],
        cwd=m3.BACKEND,
        env=env(),
        stdout=open(m3.LOGS / f"{kind}.log", "w"),  # noqa: SIM115 - child process owns the log
        stderr=subprocess.STDOUT,
    )


def start_api() -> None:
    port = m3.API.rsplit(":", 1)[1]
    subprocess.Popen(  # noqa: S603
        [
            f"{m3.VENV}/bin/uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            port,
            "--no-server-header",
        ],
        cwd=m3.BACKEND,
        env=env(),
        stdout=open(m3.LOGS / "api.log", "w"),  # noqa: SIM115 - child process owns the log
        stderr=subprocess.STDOUT,
    )
    m3.wait_http(f"{m3.API}/health")


def demo() -> Path:
    before = set((m3.REPO / "var").glob("m6-demo-*.json"))
    (m3.REPO / "var").mkdir(exist_ok=True)
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(m3.REPO / "scripts" / "m6_demo.py"),
            "--api",
            m3.API,
            "--mailpit",
            MAILPIT,
            "--credentials",
            str(m3.SECRETS / "dev-credentials.json"),
        ],
        cwd=m3.REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    (m3.OUT / "m6-demo-stdout.txt").write_text(done.stdout + "\n" + done.stderr)
    print(done.stdout[-4000:], done.stderr[-2000:], sep="\n")
    STEPS["m6_demo_exit"] = done.returncode
    new = sorted(set((m3.REPO / "var").glob("m6-demo-*.json")) - before)
    if not new:
        raise SystemExit(f"m6_demo.py wrote no evidence (exit {done.returncode}); see stdout")
    target = m3.OUT / "m6-demo-real-model.json"
    shutil.copy(new[-1], target)
    return target


# Stream events that may legitimately carry no inference label (they relay model
# output and citations; the run's mode is on the status and done events).
UNLABELLED_OK = ("token", "citation")


def inference_problems(events: list[dict[str, Any]]) -> list[str]:
    """Every reason the report stream is not provably real-model (gateway) output.

    Required: at least one ``status`` event; exactly one ``done`` event, and it is the
    last event; an explicit ``inference: "gateway"`` on the done event (and
    ``verification.inference_mode == "gateway"``) and on every status event. ``token``
    and ``citation`` events may be unlabelled; any other event type must be labelled.
    A label anywhere that is not ``gateway`` (for example ``fake``) is a problem.
    Missing labels are never dropped, so one labelled event cannot vouch for the rest.
    """
    problems: list[str] = []
    if not events:
        return ["no report events"]
    kinds = [e.get("event") for e in events]
    if kinds.count("done") != 1:
        problems.append(f"expected exactly one done event, found {kinds.count('done')}")
    elif kinds[-1] != "done":
        problems.append(f"done is not the last event (last is {kinds[-1]!r})")
    if "status" not in kinds:
        problems.append("no status events")
    for i, event in enumerate(events):
        kind = event.get("event")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        label = data.get("inference")
        if label is None and kind in UNLABELLED_OK:
            continue
        if label != "gateway":
            problems.append(f"event {i} ({kind}): inference={label!r}, expected 'gateway'")
        if kind == "done":
            mode = (data.get("verification") or {}).get("inference_mode")
            if mode != "gateway":
                problems.append(f"event {i} (done): verification.inference_mode={mode!r}")
    return problems


def checks(evidence: Path) -> bool:
    log = json.loads(evidence.read_text())
    events = log["report"]["events"]
    done = next((e["data"] for e in events if e["event"] == "done"), {})
    problems = inference_problems(events)
    labelled = sorted(
        {
            str(e["data"]["inference"])
            for e in events
            if isinstance(e.get("data"), dict) and "inference" in e["data"]
        }
    )
    total = m3.get_json(f"{MAILPIT}/api/v1/messages")["total"]  # type: ignore[index]
    result = {
        "report_outcome": done.get("outcome"),
        "report_verification_label": (done.get("verification") or {}).get("label"),
        "report_inference_labels_seen": labelled,
        "report_inference_problems": problems,
        "report_seconds": log["report"]["seconds"],
        "final_status": log["final"]["status"],
        "final_version": log["final"]["version"],
        "mailpit_total_messages": total,
        "mailpit_message_id": log["mailpit"]["message_id"],
    }
    result["pass"] = (
        STEPS.get("m6_demo_exit") == 0
        and done.get("outcome") == "published"
        and not problems
        and log["final"]["status"] == "delivered"
        and total == 1
    )
    (m3.OUT / "checks.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return bool(result["pass"])


def bundle() -> Path:
    versions = {
        "application_commit": m3.sh(f"git -C {m3.REPO} rev-parse HEAD").stdout.strip(),
        "worktree_clean": m3.sh(f"git -C {m3.REPO} status --porcelain").stdout.strip() == "",
        "gateway_health": m3.get_json(f"{m3.GATEWAY}/health"),
        "gpu": m3.sh(
            "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader",
            check=False,
        ).stdout.strip(),
        "mailpit": MAILPIT_VERSION,
        "settings": {
            "MODEL_MODE": "gateway",
            "REPORT_DISPATCH": "inline",
            "delivery": "API inline outbox dispatch; Celery worker + beat for recovery",
            "network": "loopback only",
        },
    }
    (m3.OUT / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
    for log in m3.LOGS.glob("*.log"):
        shutil.copy(log, m3.OUT / f"log-{log.name}")
    needles = [p.read_text() for p in m3.SECRETS.iterdir() if p.is_file() and p.suffix == ""]
    needles.append(os.environ["GATEWAY_TOKEN"])
    creds = json.loads((m3.SECRETS / "dev-credentials.json").read_text())
    needles += [
        pw
        for users in creds.values()
        if isinstance(users, dict)
        for pw in users.values()
        if isinstance(pw, str)
    ]
    files = sorted(m3.OUT.iterdir())
    leaks = [f.name for f in files if any(n and n in f.read_text(errors="ignore") for n in needles)]
    if leaks:
        raise SystemExit(f"secret scan failed; not bundling: {leaks}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"/content/m7-smoke-evidence-{stamp}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"m7-smoke-evidence-{stamp}/{f.name}")
    print(f"secret scan clean; wrote {target}")
    return target


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step == "vllm-env":
        m3.vllm_env()
        return
    for d in (m3.OUT, m3.LOGS):
        d.mkdir(parents=True, exist_ok=True)
    Path("/content/m3-run/logs").mkdir(parents=True, exist_ok=True)  # M3 helper's Qdrant log
    health = m3.get_json(f"{m3.GATEWAY}/health")
    (m3.OUT / "gateway-health.json").write_text(json.dumps(health, indent=2) + "\n")
    m3.services()
    m3.backend()
    m3.sh(
        f"{m3.VENV}/bin/python -m app.cli import-business-data",
        "backend.log",
        env=env(),
        cwd=m3.BACKEND,
    )
    mailpit()
    start_api()
    celery("worker")
    celery("beat")
    time.sleep(10)
    evidence = demo()
    ok = checks(evidence)
    bundle()
    if not ok:
        raise SystemExit("smoke test FAILED; see checks.json in the bundle")
    print("smoke test PASSED")


if __name__ == "__main__":
    main()
