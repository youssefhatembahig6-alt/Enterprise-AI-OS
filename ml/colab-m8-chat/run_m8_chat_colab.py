"""M8: the chat UI on the real Qwen gateway, tested in one Colab T4 runtime (loopback only).

Everything runs inside the runtime and talks over 127.0.0.1; no tunnel is opened:

  gateway   ml/model-gateway (Qwen3-8B-AWQ via vLLM, BGE-M3, dev checker), started by the
            notebook's launch cell, 127.0.0.1:8100
  services  PostgreSQL 17, Redis, Qdrant (ml/colab-m3/run_m3_colab.py helpers)
  backend   hash-locked venv; migrate (to 0017_chat_history); seed-dev (NileTech, synthetic,
            General pack on); the synthetic NileTech sales data (import-business-data) for
            the verified SQL report; API on 127.0.0.1:18000 with MODEL_MODE=gateway, inline
            dispatch
  corpus    scripts/m3_demo.py seed: the synthetic M3 documents with their permissions,
            embedded by the real BGE-M3 (Docling off: the PDFs use the documented fallback)
  web       Node 20.19.2 (checksum-verified), npm ci, Vite build of apps/web with
            VITE_API_BASE_URL=http://127.0.0.1:18000, served on 127.0.0.1:13000
  browser   Playwright 1.55.0 headless Chromium runs scripts/m8_chat_ui_check.py
            --expect-inference gateway with automatic routing (no source picker): "hello"
            and a fresh question answered by General, a fresh annual-leave question by
            Documents (cited, passage opened), a sales question by the verified SQL report
            with its email-for-approval action, a clarifying question, the sales reader's
            restricted question refused, and saved chats reloaded, reopened and deleted
  bundle    /content/m8-chat-evidence-<stamp>.zip after a secret scan

Private preview (``preview`` step, notebook cell 7): the same app, built with a relative
API base and served with its API on ONE loopback port (127.0.0.1:13100, preview_proxy.py),
which the notebook shows only to its owner with Colab's serve_kernel_port_as_window. The
step reuses the running services and API (or starts them), checks the status badge, a
fresh General question and a fresh Documents question with an opened citation in
headless Chromium on that same origin, and writes
/content/m8-chat-preview-evidence-<stamp>.zip.

    python run_m8_chat_colab.py vllm-env   # once per runtime (M1 pinned vLLM venv)
    # launch the gateway (notebook cell 3)
    python run_m8_chat_colab.py all
    python run_m8_chat_colab.py preview    # private same-origin preview + its browser check
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("M3_RUN_DIR", "/content/m8-run")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "colab-m3"))
import run_m3_colab as m3  # noqa: E402

WEB = "http://127.0.0.1:13000"
PREVIEW_PORT = 13100
PREVIEW = f"http://127.0.0.1:{PREVIEW_PORT}"
# Fresh wording per preview run (picked by the UTC minute), each answerable from the
# synthetic annual-leave policy; and open-ended General prompts.
DOCUMENTS_QUESTIONS = (
    "How many unused leave days can I carry into next year, and when do they expire?",
    "What is the annual leave allowance for staff in Egypt after ten years of service?",
    "Who do I contact in HR about family leave?",
    "Are public holidays that fall during my approved leave deducted from my allowance?",
)
GENERAL_QUESTIONS = (
    "Give me three tips for writing a clear project status email.",
    "Explain in plain words what a confusion matrix is.",
    "Suggest a simple agenda for a 30-minute weekly team retrospective.",
    "What are two common causes of slow SQL queries and how can I spot them?",
)
NODE_VERSION = "v20.19.2"
NODE_DIR = Path(f"/content/node-{NODE_VERSION}-linux-x64")
PW_VENV = Path("/content/venv-playwright-uv")
PLAYWRIGHT = "1.55.0"
STEPS: dict[str, object] = {}


# T4 run of 27 Nov (var/m7-acceptance/2026-11-28-real-chat-ui.md): with the default 60 s read
# timeout and 240 s deadline the first Documents and General answers on a cold T4 ended
# with model_unavailable/timeout; the passing retry used these values plus one warm-up call.
READ_TIMEOUT = "180"
CALL_DEADLINE = "360"


def env(**extra: str) -> dict[str, str]:
    return m3.app_env(
        CORS_ALLOWED_ORIGINS=WEB,
        USE_DOCLING="false",
        MODEL_READ_TIMEOUT_SECONDS=READ_TIMEOUT,
        MODEL_CALL_DEADLINE_SECONDS=CALL_DEADLINE,
        **extra,
    )


def warm_gateway() -> None:
    """One short synthetic chat call so the first browser question does not pay vLLM's
    cold-start (CUDA graphs, caches). Its output is discarded."""

    body = json.dumps(
        {
            "model": "Qwen/Qwen3-8B-AWQ",
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - loopback gateway
        f"{m3.GATEWAY}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['GATEWAY_TOKEN']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        resp.read()
    STEPS["gateway_warmed"] = True


def start_api() -> subprocess.Popen[bytes]:
    port = m3.API.rsplit(":", 1)[1]
    proc = subprocess.Popen(  # noqa: S603
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
    return proc


def import_business_data() -> None:
    """The synthetic NileTech sales tables behind the verified SQL report."""

    m3.sh(
        f"{m3.VENV}/bin/python -m app.cli import-business-data",
        "backend.log",
        env=env(),
        cwd=m3.BACKEND,
    )


def seed_corpus() -> None:
    m3.sh(
        f"python3 scripts/m3_demo.py seed --api {m3.API} "
        f"--credentials {m3.SECRETS}/dev-credentials.json --timeout 3600",
        "corpus.log",
        cwd=m3.REPO,
    )


def node() -> Path:
    """Node from nodejs.org, verified against the release's SHASUMS256.txt."""

    if not (NODE_DIR / "bin" / "node").exists():
        name = f"node-{NODE_VERSION}-linux-x64.tar.xz"
        base = f"https://nodejs.org/dist/{NODE_VERSION}"
        sums = urllib.request.urlopen(f"{base}/SHASUMS256.txt", timeout=60).read().decode()  # noqa: S310
        expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(name))
        archive = Path("/content") / name
        urllib.request.urlretrieve(f"{base}/{name}", archive)  # noqa: S310
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"node archive checksum mismatch: {actual} != {expected}")
        with tarfile.open(archive) as tar:
            tar.extractall("/content", filter="data")
    return NODE_DIR / "bin"


def build_web() -> None:
    bin_dir = node()
    path = f"{bin_dir}:{os.environ['PATH']}"
    web_env = {**os.environ, "PATH": path, "VITE_API_BASE_URL": m3.API}
    web_env.pop("VITE_FETCH_CREDENTIALS", None)  # cookies only in the preview build
    m3.sh("npm ci --no-audit --no-fund", "web.log", env=web_env, cwd=m3.REPO)
    m3.sh("npm run build -w @eaios/web", "web.log", env=web_env, cwd=m3.REPO)


def serve_web() -> None:
    dist = m3.REPO / "apps" / "web" / "dist"
    subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "http.server",
            WEB.rsplit(":", 1)[1],
            "--bind",
            "127.0.0.1",
            "--directory",
            str(dist),
        ],
        stdout=open(m3.LOGS / "web.log", "a"),  # noqa: SIM115 - child process owns the log
        stderr=subprocess.STDOUT,
    )
    m3.wait_http(f"{WEB}/")


def playwright() -> Path:
    """Pinned Playwright in a uv venv. Colab's ``python3 -m venv`` fails at ensurepip
    (T4 run of 27 Nov), so the venv is made with uv from the vLLM environment's Python."""

    py = PW_VENV / "bin" / "python"
    if not py.exists():
        base = Path("/content/vllm-env/bin/python")
        interpreter = str(base) if base.exists() else "3.12"
        m3.sh(f"uv venv --python {interpreter} {PW_VENV}", "playwright.log")
        m3.sh(f"uv pip install --python {py} playwright=={PLAYWRIGHT}", "playwright.log")
        m3.sh(
            f"{PW_VENV}/bin/playwright install --with-deps chromium-headless-shell",
            "playwright.log",
        )
    return py


REPORT = "m8-chat-ui-check.json"


def check_verdict(returncode: int, out: Path, run_id: str) -> tuple[bool, list[str]]:
    """Pass only on exit status 0 AND a passing report written by this very run.

    A report left by an earlier run, or one that says pass while the process failed,
    never counts.
    """

    reasons: list[str] = []
    if returncode != 0:
        reasons.append(f"browser check exited {returncode}")
    report = out / REPORT
    if not report.is_file():
        reasons.append(f"no {REPORT} from this run")
        return False, reasons
    try:
        data = json.loads(report.read_text())
    except (OSError, ValueError):
        reasons.append(f"{REPORT} is unreadable")
        return False, reasons
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        reasons.append(f"{REPORT} is not from this run")
    elif data.get("pass") is not True:
        reasons.append(f"{REPORT} reports failure")
    return not reasons, reasons


def ui_check(
    py: Path, web: str = WEB, api: str = m3.API, name: str = "ui", extra: tuple[str, ...] = ()
) -> dict[str, object]:
    """Run scripts/m8_chat_ui_check.py into a NEW folder <name>/<run_id> and judge that run."""

    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{secrets.token_hex(4)}"
    out = m3.OUT / name / run_id
    out.mkdir(parents=True)  # raises if it exists: every check writes fresh evidence
    done = subprocess.run(  # noqa: S603
        [
            str(py),
            str(m3.REPO / "scripts" / "m8_chat_ui_check.py"),
            "--web",
            web,
            "--api",
            api,
            "--credentials",
            str(m3.SECRETS / "dev-credentials.json"),
            "--out",
            str(out),
            "--run-id",
            run_id,
            "--expect-inference",
            "gateway",
            "--timeout",
            "420",
            *extra,
        ],
        cwd=m3.REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    (out / "check-stdout.txt").write_text(done.stdout + "\n" + done.stderr)
    print(done.stdout[-4000:], done.stderr[-2000:], sep="\n")
    passed, reasons = check_verdict(done.returncode, out, run_id)
    evidence = str((out / REPORT).relative_to(m3.OUT))
    STEPS[f"{name}_check"] = {
        "run_id": run_id,
        "exit": done.returncode,
        "evidence": evidence,
        "pass": passed,
        "reasons": reasons,
    }
    return {"pass": passed, "run_id": run_id, "evidence": evidence, "reasons": reasons}


def bundle(name: str = "m8-chat-evidence") -> Path:
    versions = {
        "application_commit": m3.sh(f"git -C {m3.REPO} rev-parse HEAD").stdout.strip(),
        "worktree_clean": m3.sh(f"git -C {m3.REPO} status --porcelain").stdout.strip() == "",
        "gateway_health": m3.get_json(f"{m3.GATEWAY}/health"),
        "gpu": m3.sh(
            "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader",
            check=False,
        ).stdout.strip(),
        "node": NODE_VERSION,
        "playwright": PLAYWRIGHT,
        "settings": {
            "MODEL_MODE": "gateway",
            "REPORT_DISPATCH": "inline",
            "USE_DOCLING": "false",
            "MODEL_READ_TIMEOUT_SECONDS": READ_TIMEOUT,
            "MODEL_CALL_DEADLINE_SECONDS": CALL_DEADLINE,
            "network": "loopback only: web 127.0.0.1:13000, API 127.0.0.1:18000, "
            "gateway 127.0.0.1:8100",
        },
        "steps": STEPS,
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
    files = sorted(p for p in m3.OUT.rglob("*") if p.is_file())
    leaks = [
        str(f.relative_to(m3.OUT))
        for f in files
        if f.suffix != ".png" and any(n and n in f.read_text(errors="ignore") for n in needles)
    ]
    if leaks:
        raise SystemExit(f"secret scan failed; not bundling: {leaks}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"/content/{name}-{stamp}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"{name}-{stamp}/{f.relative_to(m3.OUT)}")
    print(f"secret scan clean; wrote {target}")
    return target


def api_up() -> bool:
    try:
        m3.get_json(f"{m3.API}/health")
    except OSError:
        return False
    return True


def build_preview() -> Path:
    """The UI with an empty VITE_API_BASE_URL: it calls /api/... on its own origin.

    VITE_FETCH_CREDENTIALS=same-origin lets the browser attach the Colab session cookie that
    Colab's port window requires on every request to that origin (apps/web/src/fetchPolicy.ts).
    Authentication to the app is still the bearer token; preview_proxy.py drops cookies
    before forwarding to the API.
    """

    bin_dir = node()
    web_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VITE_API_BASE_URL": "",
        "VITE_FETCH_CREDENTIALS": "same-origin",
    }
    if not (m3.REPO / "node_modules").exists():
        m3.sh("npm ci --no-audit --no-fund", "preview.log", env=web_env, cwd=m3.REPO)
    m3.sh(
        "npm run build -w @eaios/web -- --outDir dist-preview",
        "preview.log",
        env=web_env,
        cwd=m3.REPO,
    )
    return m3.REPO / "apps" / "web" / "dist-preview"


def serve_preview(dist: Path) -> None:
    try:
        m3.get_json(f"{PREVIEW}/health")
        return  # already serving (re-run of the cell)
    except OSError:
        pass
    subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(HERE / "preview_proxy.py"),
            "--dist",
            str(dist),
            "--port",
            str(PREVIEW_PORT),
            "--api",
            m3.API.removeprefix("http://"),
        ],
        stdout=open(m3.LOGS / "preview-proxy.log", "a"),  # noqa: SIM115 - child owns the log
        stderr=subprocess.STDOUT,
    )
    m3.wait_http(f"{PREVIEW}/health")


def preview() -> None:
    """Private same-origin preview on 127.0.0.1:13100 and its browser check."""

    if not api_up():  # fresh runtime: same setup as ``all`` (minus the 13000 test build)
        m3.services()
        m3.backend()
        import_business_data()
        start_api()
        seed_corpus()
    serve_preview(build_preview())
    py = playwright()
    warm_gateway()
    minute = int(datetime.now(UTC).timestamp() // 60)
    documents = DOCUMENTS_QUESTIONS[minute % len(DOCUMENTS_QUESTIONS)]
    general = GENERAL_QUESTIONS[minute % len(GENERAL_QUESTIONS)]
    result = ui_check(
        py,
        web=PREVIEW,
        api=PREVIEW,
        name="preview",
        extra=("--documents-question", documents, "--general-question", general),
    )
    STEPS["preview"] = {
        "url": PREVIEW,
        "documents_question": documents,
        "general_question": general,
    }
    bundle("m8-chat-preview-evidence")
    if not result["pass"]:
        reasons = "; ".join(result["reasons"])
        raise SystemExit(f"preview check FAILED ({reasons}); see {result['evidence']}")
    print(f"preview check PASSED on the real gateway; serving {PREVIEW} (loopback only)")


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
    if step == "preview":
        preview()
        return
    m3.services()
    m3.backend()
    import_business_data()
    start_api()
    seed_corpus()
    build_web()
    serve_web()
    py = playwright()
    warm_gateway()
    result = ui_check(py)
    bundle()
    if not result["pass"]:
        reasons = "; ".join(result["reasons"])
        raise SystemExit(f"UI check FAILED ({reasons}); see {result['evidence']}")
    print("UI check PASSED on the real gateway")


if __name__ == "__main__":
    main()
