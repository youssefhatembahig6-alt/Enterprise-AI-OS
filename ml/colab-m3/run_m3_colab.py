"""M3 acceptance run in one Colab T4 runtime, everything over loopback (as in M2).

Prerequisites (see README.md): the repository is cloned at the PR head in
/content/Enterprise-AI-OS, the Docling venv exists (``docling-env`` below), and the
model gateway is serving on 127.0.0.1:8100 with ``DOCLING_PYTHON`` set
(``ml/model-gateway/colab_launch.py``). ``GATEWAY_TOKEN`` must be in the environment.

    python run_m3_colab.py vllm-env         # M1's pinned vLLM venv (/content/vllm-env)
    python run_m3_colab.py docling-env      # before launching the gateway
    python run_m3_colab.py all              # services, API, measurements, bundle

``all`` runs, in order:
  services   PostgreSQL 17 (PGDG), Redis, Qdrant v1.15.4, all on 127.0.0.1
  backend    Python 3.13 venv from requirements.lock (hash-checked), migrate, seed-dev
  api        uvicorn on 127.0.0.1:18000, MODEL_MODE=gateway, inline dispatch
  demo       scripts/m3_demo.py seed, wait for ingestion, scripts/m3_demo.py capture
  measure    retrieval-eval (dense and hybrid, Docling-parsed corpus), relevance-study
  parsers    parser-export + M1 scorer on the PDFs as the application stored them
  fallback   a second API with Docling unreachable; the three PDFs uploaded without
             permissions (never searchable); parser-export + M1 scorer again
  bundle     versions, logs and outputs into /content/m3-evidence-<stamp>.zip after a
             secret scan (credentials and tokens never leave /content/m3-run/secrets)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(os.environ.get("EAIOS_REPO", "/content/Enterprise-AI-OS"))
BACKEND = REPO / "backend"
RUN = Path(os.environ.get("M3_RUN_DIR", "/content/m3-run"))
SECRETS = RUN / "secrets"
OUT = RUN / "out"
LOGS = RUN / "logs"
VENV = Path("/content/venv-backend")
DOCLING_VENV = Path("/content/venv-docling")
QDRANT_VERSION = "v1.15.4"
API = "http://127.0.0.1:18000"
FALLBACK_API = "http://127.0.0.1:18001"
GATEWAY = "http://127.0.0.1:8100"
QDRANT = "http://127.0.0.1:6333"
FALLBACK_TITLE = " (fallback check)"
PDFS = ("two-column-newsletter.pdf", "table-heavy-sales-q1-2026.pdf", "scanned-delivery-note.pdf")


def sh(cmd: str, log: str | None = None, env: dict[str, str] | None = None,
       cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f"+ {cmd}", flush=True)
    done = subprocess.run(cmd, shell=True, text=True, capture_output=True, env=env,  # noqa: S602
                          cwd=cwd, check=False)
    if log:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / log, "a") as fh:
            fh.write(f"$ {cmd}\n{done.stdout}\n{done.stderr}\n")
    if check and done.returncode != 0:
        print(done.stdout[-3000:], done.stderr[-3000:], sep="\n")
        raise SystemExit(f"command failed ({done.returncode}): {cmd}")
    return done


def secret(name: str) -> str:
    SECRETS.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS, 0o700)
    path = SECRETS / name
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
        os.chmod(path, 0o600)
    return path.read_text()


def wait_http(url: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):  # noqa: S310 - loopback
                return
        except urllib.error.HTTPError:
            return  # it answered
        except OSError:
            time.sleep(2)
    raise SystemExit(f"{url} did not come up")


def get_json(url: str, token: str | None = None) -> object:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:  # noqa: S310
        return json.loads(r.read())


# -- environment pieces ----------------------------------------------------------------------


def vllm_env() -> None:
    """The measured vLLM environment, installed as the M1 GPU notebook does (uv venv,
    Python 3.12, ml/gpu-feasibility/vllm-env-pins.txt); kernel-side helpers for the
    gateway (BGE-M3 and the checker use Colab's own torch and transformers)."""

    sh("pip -q install uv==0.9.5 fastapi==0.116.1 uvicorn==0.35.0 httpx==0.28.1 "
       "sentencepiece protobuf", "vllm-env.log")
    venv = Path("/content/vllm-env")
    if not (venv / "bin" / "python").exists():
        sh(f"uv venv --python 3.12 {venv}", "vllm-env.log")
    sh(f"uv pip install --python {venv}/bin/python "
       f"-r {REPO}/ml/gpu-feasibility/vllm-env-pins.txt", "vllm-env.log")
    print(sh(f"{venv}/bin/python -c 'import vllm, transformers; "
             "print(vllm.__version__, transformers.__version__)'").stdout)


def docling_env() -> None:
    """The M1 Docling environment (same pins and Python as the M1 parser benchmark)."""

    sh("apt-get -qq update && apt-get -qq install -y tesseract-ocr poppler-utils libmagic1 > /dev/null")
    sh("pip -q install uv==0.9.5")
    if not (DOCLING_VENV / "bin" / "python").exists():
        sh(f"uv venv --python 3.12 {DOCLING_VENV}", "docling-env.log")
    sh(f"uv pip install --python {DOCLING_VENV}/bin/python "
       f"-r {REPO}/ml/parser-benchmark/colab-docling-pins.txt", "docling-env.log")
    sh(f"{DOCLING_VENV}/bin/python -c 'import docling.document_converter, rapidfuzz'", "docling-env.log")
    print(f"Docling ready. Before launching the gateway: os.environ['DOCLING_PYTHON'] = "
          f"'{DOCLING_VENV}/bin/python'")


def services() -> None:
    if not Path("/usr/lib/postgresql/17/bin/postgres").exists():
        sh("apt-get -qq update && apt-get -qq install -y postgresql-common > /dev/null", "services.log")
        sh("/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y > /dev/null", "services.log")
        sh("apt-get -qq install -y postgresql-17 redis-server > /dev/null", "services.log")
    sh("pg_ctlcluster 17 main start || true", "services.log", check=False)
    owner, app, report = secret("owner_pw"), secret("app_pw"), secret("report_pw")
    sql = """
SELECT 'CREATE ROLE eaios_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eaios_owner') \\gexec
SELECT 'CREATE ROLE eaios_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eaios_app') \\gexec
ALTER ROLE eaios_owner PASSWORD :'owner_pw';
ALTER ROLE eaios_app PASSWORD :'app_pw';
SELECT 'CREATE DATABASE eaios OWNER eaios_owner'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'eaios') \\gexec
REVOKE ALL ON DATABASE eaios FROM PUBLIC;
GRANT CONNECT ON DATABASE eaios TO eaios_app;
"""
    (SECRETS / "roles.sql").write_text(sql)
    shutil.copy(REPO / "infra/postgres/report-role.sql", SECRETS / "report-role.sql")
    os.chmod(SECRETS, 0o755)  # postgres must read the SQL files; values come via -v
    for f in ("roles.sql", "report-role.sql"):
        os.chmod(SECRETS / f, 0o644)
    # Passwords reach psql through the environment, so the echoed commands hold none.
    pw_env = {**os.environ, "OWNER_PW": owner, "APP_PW": app, "REPORT_PW": report}
    psql = "runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q"
    sh(f'{psql} -d postgres -v owner_pw="$OWNER_PW" -v app_pw="$APP_PW" -f {SECRETS}/roles.sql',
       env=pw_env)
    sh(f'{psql} -d postgres -v report_pw="$REPORT_PW" -f {SECRETS}/report-role.sql', env=pw_env)
    sh(f"{psql} -d eaios -c 'REVOKE CREATE ON SCHEMA public FROM PUBLIC'")
    os.chmod(SECRETS, 0o700)
    redis_env = {**os.environ, "REDISCLI_AUTH": secret("redis_pw")}
    sh('redis-cli ping | grep -q PONG || redis-server --daemonize yes --bind 127.0.0.1 '
       '--port 6379 --requirepass "$REDISCLI_AUTH"', env=redis_env, check=False)
    qdrant_bin = Path("/content/qdrant")
    if not qdrant_bin.exists():
        sh(f"cd /content && wget -q https://github.com/qdrant/qdrant/releases/download/"
           f"{QDRANT_VERSION}/qdrant-x86_64-unknown-linux-gnu.tar.gz && "
           "tar xzf qdrant-x86_64-unknown-linux-gnu.tar.gz", "services.log")
    try:
        get_json(f"{QDRANT}/")
    except OSError:
        sh("cd /content && (ulimit -n 65536; QDRANT__SERVICE__HOST=127.0.0.1 "
           "QDRANT__STORAGE__STORAGE_PATH=/content/qdrant-storage "
           "nohup ./qdrant > /content/m3-run/logs/qdrant.log 2>&1 &)")
    wait_http(f"{QDRANT}/readyz")


def app_env(**extra: str) -> dict[str, str]:
    token = os.environ.get("GATEWAY_TOKEN")
    if not token:
        raise SystemExit("GATEWAY_TOKEN is not set (the gateway's bearer token)")
    db = "127.0.0.1:5432/eaios"
    env = {
        "PATH": f"{VENV}/bin:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/root"),
        "EAIOS_ENV": "development",
        "DATABASE_URL": f"postgresql+psycopg://eaios_app:{secret('app_pw')}@{db}",
        "MIGRATION_DATABASE_URL": f"postgresql+psycopg://eaios_owner:{secret('owner_pw')}@{db}",
        "REPORT_DATABASE_URL": f"postgresql+psycopg://eaios_report:{secret('report_pw')}@{db}",
        "REDIS_URL": f"redis://:{secret('redis_pw')}@127.0.0.1:6379/0",
        "QDRANT_URL": QDRANT,
        "S3_ENDPOINT_URL": "http://127.0.0.1:1",  # unused: filesystem document store
        "DOCUMENT_STORE": "filesystem",
        "DOCUMENT_FS_ROOT": str(RUN / "documents"),
        "JWT_SECRET": secret("jwt_secret") + secret("jwt_secret_2"),
        "REPORT_DISPATCH": "inline",
        "MODEL_MODE": "gateway",
        "LLM_BASE_URL": GATEWAY,
        "MODEL_GATEWAY_TOKEN": token,
        "RETRIEVAL_MODE": os.environ.get("RETRIEVAL_MODE", "dense"),
        "RELEVANCE_FILTER": os.environ.get("RELEVANCE_FILTER", "lexical"),
        "RELEVANCE_MIN_SCORE": os.environ.get("RELEVANCE_MIN_SCORE", "0.0"),
        "DOCLING_TIMEOUT_SECONDS": "900",
        "INGESTION_JOB_LEASE_SECONDS": "1800",
        "CORS_ALLOWED_ORIGINS": "http://127.0.0.1:3000",
    }
    env.update(extra)
    return env


def backend() -> None:
    sh("pip -q install uv==0.9.5")
    if not (VENV / "bin" / "python").exists():
        sh(f"uv venv --python 3.13 {VENV}", "backend.log")
    sh(f"uv pip install --python {VENV}/bin/python --require-hashes -r {BACKEND}/requirements.lock",
       "backend.log")
    env = app_env()
    sh(f"{VENV}/bin/python -m app.cli migrate", "backend.log", env=env, cwd=BACKEND)
    sh(f"{VENV}/bin/python -m app.cli seed-dev --credentials-file {SECRETS}/dev-credentials.json",
       "backend.log", env=env, cwd=BACKEND)


def start_api(url: str, log: str, **extra: str) -> subprocess.Popen[bytes]:
    port = url.rsplit(":", 1)[1]
    LOGS.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(  # noqa: S603
        [f"{VENV}/bin/uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
         "--port", port, "--no-server-header"],
        cwd=BACKEND, env=app_env(**extra),
        stdout=open(LOGS / log, "w"), stderr=subprocess.STDOUT,  # noqa: SIM115
    )
    wait_http(f"{url}/health")
    print(f"API on {url} (pid {proc.pid})")
    return proc


def demo() -> None:
    creds = f"--credentials {SECRETS}/dev-credentials.json"
    sh(f"python3 scripts/m3_demo.py seed --api {API} {creds} --timeout 3600", "demo.log", cwd=REPO)
    capture = sh(f"python3 scripts/m3_demo.py capture --api {API} {creds}", "demo.log", cwd=REPO)
    print(capture.stdout)
    written = sorted((REPO / "var").glob("m3-demo-*.json"))[-1]
    shutil.copy(written, OUT / "m3-demo-colab.json")


def measure() -> None:
    env = app_env()
    r = sh(f"{VENV}/bin/python -m app.cli retrieval-eval --docling --qdrant {QDRANT} "
           f"--out {OUT}/retrieval-eval.json", "measure.log", env=env, cwd=BACKEND)
    (OUT / "retrieval-eval.md").write_text(r.stdout)
    r = sh(f"{VENV}/bin/python -m app.cli relevance-study --out {OUT}/relevance-study.json",
           "measure.log", env=env, cwd=BACKEND)
    (OUT / "relevance-study.md").write_text(r.stdout)
    print((OUT / "retrieval-eval.md").read_text(), (OUT / "relevance-study.md").read_text())


def score_parsers(name: str) -> None:
    sh(f"{VENV}/bin/python -m app.cli parser-export --out {OUT}/{name}-export.json",
       "parsers.log", env=app_env(), cwd=BACKEND)
    r = sh(f"{DOCLING_VENV}/bin/python -m bench.score_app {OUT}/{name}-export.json "
           f"--out {OUT}/{name}-scores.json", "parsers.log", cwd=REPO / "ml/parser-benchmark")
    (OUT / f"{name}-scores.md").write_text(r.stdout)
    print(r.stdout)


def wait_documents(titles: set[str], timeout: float = 3600.0) -> None:
    import_sql = ("SELECT d.title, v.status FROM documents d JOIN document_versions v"
                  " ON v.document_id = d.id AND v.version = d.current_version")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = sh(f"{VENV}/bin/python - <<'EOF'\nimport os, sqlalchemy as sa\n"
                 f"e = sa.create_engine(os.environ['MIGRATION_DATABASE_URL'])\n"
                 f"with e.connect() as c:\n    rows = c.execute(sa.text(\"{import_sql}\")).all()\n"
                 "print('\\n'.join(f'{t}\\t{s}' for t, s in rows))\nEOF",
                 env=app_env(), cwd=BACKEND).stdout
        statuses = dict(line.split("\t") for line in out.splitlines() if "\t" in line)
        pending = [t for t in titles if statuses.get(t) in (None, "queued", "parsing", "indexing")]
        if not pending:
            return
        print(f"waiting for {pending}")
        time.sleep(10)
    raise SystemExit("fallback uploads did not finish")


def fallback(api_proc: subprocess.Popen[bytes] | None) -> None:
    """Docling unreachable (the gateway answers 404 on the parse path): the documented
    fallback must apply. Uploaded without permissions, so never searchable."""

    if api_proc is not None:
        api_proc.terminate()
        api_proc.wait(30)
    proc = start_api(FALLBACK_API, "api-fallback.log", MODEL_PARSE_PATH="/parse-unavailable")
    try:
        creds = json.loads((SECRETS / "dev-credentials.json").read_text())
        email = "access.admin@niletech.example"
        body = {"tenant": "niletech", "email": email, "password": creds["niletech"][email]}
        req = urllib.request.Request(f"{FALLBACK_API}/api/v1/auth/login", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
        token = json.loads(urllib.request.urlopen(req, timeout=60).read())["access_token"]  # noqa: S310
        titles = set()
        for name in PDFS:
            data = (BACKEND / "fixtures/documents/files" / name).read_bytes()
            title = name + FALLBACK_TITLE
            titles.add(title)
            upload = {"filename": name, "content_base64": base64.b64encode(data).decode(),
                      "department": "finance", "title": title}
            req = urllib.request.Request(
                f"{FALLBACK_API}/api/v1/documents", json.dumps(upload).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                method="POST")
            print(name, json.loads(urllib.request.urlopen(req, timeout=60).read()))  # noqa: S310
        wait_documents(titles)
    finally:
        proc.terminate()
        proc.wait(30)
    score_parsers("parsers-after-fallback")


def versions() -> dict[str, object]:
    def out(cmd: str) -> str:
        return sh(cmd, check=False).stdout.strip()

    health = get_json(f"{GATEWAY}/health")
    return {
        "application_commit": out(f"git -C {REPO} rev-parse HEAD"),
        "worktree_clean": out(f"git -C {REPO} status --porcelain") == "",
        "gateway_health": health,
        "postgresql": out("/usr/lib/postgresql/17/bin/postgres --version"),
        "redis": out("redis-server --version"),
        "qdrant": get_json(f"{QDRANT}/"),
        "python_backend": out(f"{VENV}/bin/python --version"),
        "python_docling": out(f"{DOCLING_VENV}/bin/python --version"),
        "docling_freeze_sha256": hashlib.sha256(
            out(f"uv pip freeze --python {DOCLING_VENV}/bin/python").encode()).hexdigest(),
        "docling_packages": out(f"uv pip freeze --python {DOCLING_VENV}/bin/python | "
                                "grep -Ei '^(docling|docling-core|docling-ibm-models|easyocr|torch)=='"),
        "backend_lock_sha256": hashlib.sha256((BACKEND / "requirements.lock").read_bytes()).hexdigest(),
        "gpu": out("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"),
        "settings": {k: app_env()[k] for k in ("RETRIEVAL_MODE", "RELEVANCE_FILTER",
                                               "RELEVANCE_MIN_SCORE", "REPORT_DISPATCH",
                                               "DOCUMENT_STORE", "MODEL_MODE")},
        "network": "loopback only (API 127.0.0.1:18000, gateway 127.0.0.1:8100)",
    }


def bundle() -> Path:
    (OUT / "versions.json").write_text(json.dumps(versions(), indent=2) + "\n")
    for log in LOGS.glob("*.log"):
        shutil.copy(log, OUT / f"log-{log.name}")
    needles = [p.read_text() for p in SECRETS.iterdir() if p.is_file() and p.suffix == ""]
    needles.append(os.environ["GATEWAY_TOKEN"])
    creds = json.loads((SECRETS / "dev-credentials.json").read_text())
    needles += [pw for users in creds.values() if isinstance(users, dict)
                for pw in users.values() if isinstance(pw, str)]
    leaks = [f.name for f in OUT.iterdir() if any(n and n in f.read_text(errors="ignore") for n in needles)]
    if leaks:
        raise SystemExit(f"secret scan failed; not bundling: {leaks}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"/content/m3-evidence-{stamp}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(OUT.iterdir()):
            zf.write(f, f"m3-evidence-{stamp}/{f.name}")
    print(f"secret scan clean; wrote {target}")
    return target


def main() -> None:
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    for d in (OUT, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    if step in ("vllm-env", "docling-env"):
        vllm_env() if step == "vllm-env" else docling_env()
        return
    health = get_json(f"{GATEWAY}/health")
    print("gateway models:", json.dumps(health.get("models"), indent=1))  # type: ignore[union-attr]
    if "parser" not in health.get("models", {}):  # type: ignore[union-attr]
        raise SystemExit("gateway has no Docling parser; set DOCLING_PYTHON and relaunch it")
    services()
    backend()
    api = start_api(API, "api.log")
    demo()
    measure()
    score_parsers("parsers")
    fallback(api)
    bundle()


if __name__ == "__main__":
    main()
