"""M8: the chat UI on the real Qwen gateway, tested in one Colab T4 runtime (loopback only).

Everything runs inside the runtime and talks over 127.0.0.1; no tunnel is opened:

  gateway   ml/model-gateway (Qwen3-8B-AWQ via vLLM, BGE-M3, dev checker), started by the
            notebook's launch cell, 127.0.0.1:8100
  services  PostgreSQL 17, Redis, Qdrant (ml/colab-m3/run_m3_colab.py helpers)
  backend   hash-locked venv; migrate (to 0016_general_chat); seed-dev (NileTech, synthetic,
            General pack on); API on 127.0.0.1:18000 with MODEL_MODE=gateway, inline dispatch
  corpus    scripts/m3_demo.py seed: the synthetic M3 documents with their permissions,
            embedded by the real BGE-M3 (Docling off: the PDFs use the documented fallback)
  web       Node 20.19.2 (checksum-verified), npm ci, Vite build of apps/web with
            VITE_API_BASE_URL=http://127.0.0.1:18000, served on 127.0.0.1:13000
  browser   Playwright 1.55.0 headless Chromium runs scripts/m8_chat_ui_check.py
            --expect-inference gateway: a newly phrased annual-leave question (Documents,
            cited, passage opened), a sales reader refused the restricted finance answer,
            and a fresh open-ended prompt in General mode
  bundle    /content/m8-chat-evidence-<stamp>.zip after a secret scan

    python run_m8_chat_colab.py vllm-env   # once per runtime (M1 pinned vLLM venv)
    # launch the gateway (notebook cell 3)
    python run_m8_chat_colab.py all
"""

from __future__ import annotations

import hashlib
import json
import os
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
NODE_VERSION = "v20.19.2"
NODE_DIR = Path(f"/content/node-{NODE_VERSION}-linux-x64")
PW_VENV = Path("/content/venv-playwright")
PLAYWRIGHT = "1.55.0"
STEPS: dict[str, object] = {}


def env(**extra: str) -> dict[str, str]:
    return m3.app_env(CORS_ALLOWED_ORIGINS=WEB, USE_DOCLING="false", **extra)


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
    py = PW_VENV / "bin" / "python"
    if not py.exists():
        m3.sh(f"python3 -m venv {PW_VENV}", "playwright.log")
        m3.sh(f"{py} -m pip install -q playwright=={PLAYWRIGHT}", "playwright.log")
        m3.sh(
            f"{PW_VENV}/bin/playwright install --with-deps chromium-headless-shell",
            "playwright.log",
        )
    return py


def ui_check(py: Path) -> dict[str, object]:
    out = m3.OUT / "ui"
    done = subprocess.run(  # noqa: S603
        [
            str(py),
            str(m3.REPO / "scripts" / "m8_chat_ui_check.py"),
            "--web",
            WEB,
            "--api",
            m3.API,
            "--credentials",
            str(m3.SECRETS / "dev-credentials.json"),
            "--out",
            str(out),
            "--expect-inference",
            "gateway",
            "--timeout",
            "420",
        ],
        cwd=m3.REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    (m3.OUT / "ui-check-stdout.txt").write_text(done.stdout + "\n" + done.stderr)
    print(done.stdout[-4000:], done.stderr[-2000:], sep="\n")
    STEPS["ui_check_exit"] = done.returncode
    evidence = out / "m8-chat-ui-check.json"
    return json.loads(evidence.read_text()) if evidence.exists() else {"pass": False}


def bundle() -> Path:
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
    target = Path(f"/content/m8-chat-evidence-{stamp}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f"m8-chat-evidence-{stamp}/{f.relative_to(m3.OUT)}")
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
    start_api()
    seed_corpus()
    build_web()
    serve_web()
    result = ui_check(playwright())
    bundle()
    if not result.get("pass"):
        raise SystemExit("UI check FAILED; see ui/m8-chat-ui-check.json in the bundle")
    print("UI check PASSED on the real gateway")


if __name__ == "__main__":
    main()
