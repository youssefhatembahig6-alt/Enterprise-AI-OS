"""Build m1_gpu_feasibility.ipynb from the cell sources below.

Edit this file, not the notebook JSON, then run:

    python ml/gpu-feasibility/build_notebook.py

The notebook is self-contained so it can be opened directly from GitHub in Colab.
"""

from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
TARGET = HERE / "m1_gpu_feasibility.ipynb"

CELLS: list[tuple[str, str]] = []


def md(src: str) -> None:
    CELLS.append(("markdown", src.strip("\n")))


def code(src: str) -> None:
    CELLS.append(("code", src.strip("\n")))


# ---------------------------------------------------------------------------
md(r"""
# EAIOS M1 · GPU feasibility (Colab T4)

Two modes, chosen in cell 2. Each run produces results for **one** mode only, and the summary is labelled with it.

| Mode | Models (only these are downloaded) | What runs | Time |
| --- | --- | --- | --- |
| **`small`** (default) | Qwen3-1.7B (vLLM, fp16) · BGE-M3 · DeBERTa-v3 NLI baseline | One NileTech document question (Q05): embedding → answer generation → checking | ~15–25 min |
| `target` | Qwen3-8B-AWQ (vLLM) · BGE-M3 · DeBERTa-v3 NLI baseline | Ten timing questions ×2, concurrent load, cancellation and restart at `gpu_memory_utilization` 0.85 and 0.75 | ~70–100 min |

**To run:** choose `Runtime → Change runtime type → T4 GPU`, then `Runtime → Run all`. Approve the Google Drive prompt if it appears.

* Cell 1 checks the GPU and its memory **before** anything is installed or downloaded, and stops if no GPU is attached.
* Every stage is recorded in `results.json` as soon as it finishes, and mirrored to Drive when Drive is mounted. A failed stage is recorded with its error, and the run continues where it can.
* The last cell downloads a zip containing the results, the summary and the server logs. You can run it at any time.

**What this run is not:**

* **Not the fine-tuned checker.** The checker is a **public zero-shot NLI checkpoint** used as a development baseline (M7 replaces it). Its labels here are a smoke check, not an accuracy measurement.
* **Not a target measurement in small mode.** Small-mode numbers show only that the path works. Never compare them with target results.
* **Not exposed outside the runtime.** vLLM listens on `127.0.0.1` inside this runtime only; no tunnel or public endpoint is opened.
* **Not executing SQL.** The SQL Qwen writes is recorded but not executed. Generation uses fixed evidence, so timings stay comparable across runs.
* **Not the evaluation set.** The ten questions (`m1-gpu-workload-v1`) are accepted for repeatable timing only and are kept separate from held-out quality evaluation.
""")

# ---------------------------------------------------------------------------
code(r'''
# @title 1 · Detect the GPU and memory (nothing is installed or downloaded yet; same for both modes)
import datetime, hashlib, importlib, json, os, pathlib, platform, re, shutil, subprocess, sys, time, traceback, uuid

NOTEBOOK_VERSION = "1.0.0"
RUN_ID = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
BASE = pathlib.Path("/content") if pathlib.Path("/content").is_dir() else pathlib.Path.cwd()
OUT = BASE / "eaios_m1_gpu" / RUN_ID
OUT.mkdir(parents=True, exist_ok=True)
DRIVE_DIR = None  # set by cell 3 when Drive is mounted


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sh(cmd, timeout=600):
    """Run a command and return (exit code, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # missing binary or timeout
        return -1, f"{type(exc).__name__}: {exc}"


def detect_gpu():
    fields = ["name", "memory.total", "memory.used", "memory.free", "driver_version", "compute_cap", "pci.bus_id"]
    query = lambda f: ["nvidia-smi", "--query-gpu=" + ",".join(f), "--format=csv,noheader,nounits"]
    rc, out = sh(query(fields))
    if rc != 0:  # older drivers do not know compute_cap
        fields.remove("compute_cap")
        rc, out = sh(query(fields))
    if rc != 0 or not out.strip():
        return None, out
    gpus = []
    for line in out.strip().splitlines():
        g = dict(zip(fields, [v.strip() for v in line.split(",")]))
        for k in ("memory.total", "memory.used", "memory.free"):
            g[k.replace(".", "_") + "_mib"] = int(float(g.pop(k)))
        gpus.append(g)
    _, full = sh(["nvidia-smi"])
    m = re.search(r"CUDA Version:\s*([\d.]+)", full)
    return {"gpus": gpus, "driver_cuda_version": m.group(1) if m else None, "nvidia_smi": full}, None


def host_memory_mib():
    info = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            key, val = line.split(":", 1)
            if key in ("MemTotal", "MemAvailable"):
                info[key] = int(val.split()[0]) // 1024
    except OSError:
        pass
    return {"total_mib": info.get("MemTotal"), "available_mib": info.get("MemAvailable")}


RESULTS = {
    "schema_version": 1,
    "kind": "eaios-m1-gpu-feasibility",
    "notebook_version": NOTEBOOK_VERSION,
    "run_id": RUN_ID,
    "started_at": utcnow(),
    "environment": {},
    "config": {},
    "workload": {},
    "downloads": {},
    "stages": [],
    "configs": {},
    "failures": [],
}
STATUS = {}


def save_results():
    """Write results.json atomically and mirror the output folder to Drive if mounted."""
    RESULTS["updated_at"] = utcnow()
    tmp = OUT / "results.json.tmp"
    tmp.write_text(json.dumps(RESULTS, indent=2, default=str))
    os.replace(tmp, OUT / "results.json")
    if DRIVE_DIR is not None:
        try:
            shutil.copytree(OUT, DRIVE_DIR, dirs_exist_ok=True)
        except Exception as exc:
            print(f"[warn] Drive mirror failed: {exc}")


def record_failure(stage, kind, message, detail=None):
    RESULTS["failures"].append({"stage": stage, "kind": kind, "message": str(message)[:4000],
                                "detail": detail, "at": utcnow()})


def run_stage(name, fn, requires=(), container=None):
    """Run one stage, record status/timing/details/errors, save results. Returns the stage's return value or None."""
    entry = {"name": name, "started_at": utcnow()}
    (container if container is not None else RESULTS["stages"]).append(entry)
    missing = [r for r in requires if STATUS.get(r) != "ok"]
    if missing:
        entry.update(status="skipped", reason="prerequisite not ok: " + ", ".join(missing))
        STATUS[name] = "skipped"
        print(f"[skip] {name}: {entry['reason']}")
        save_results()
        return None
    print(f"[run ] {name} …", flush=True)
    t0 = time.perf_counter()
    try:
        value = fn()
        entry["status"] = "ok"
        if isinstance(value, dict):
            entry["details"] = value
        return value
    except KeyboardInterrupt:
        entry["status"] = "interrupted"
        raise
    except Exception as exc:
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["traceback"] = traceback.format_exc()[-6000:]
        kind = "oom" if "out of memory" in str(exc).lower() or "OutOfMemory" in type(exc).__name__ else "error"
        record_failure(name, kind, entry["error"])
        print(f"[FAIL] {name}: {entry['error']}")
        return None
    finally:
        entry["duration_s"] = round(time.perf_counter() - t0, 3)
        entry["ended_at"] = utcnow()
        STATUS[name] = entry.get("status")
        if entry["status"] == "ok":
            print(f"[ ok ] {name} ({entry['duration_s']:.1f}s)")
        save_results()


gpu_info, gpu_err = detect_gpu()
disk = shutil.disk_usage(str(BASE))
RESULTS["environment"] = {
    "detected_at": utcnow(),
    "gpu": gpu_info,
    "gpu_error": gpu_err,
    "host_memory": host_memory_mib(),
    "disk_free_gib": round(disk.free / 2**30, 1),
    "python": sys.version,
    "platform": platform.platform(),
    "colab": "google.colab" in sys.modules or os.path.exists("/content/sample_data"),
    "colab_env": {k: v for k, v in os.environ.items() if k.startswith(("COLAB_", "TPU_", "GCE_"))
                  and "TOKEN" not in k and "KEY" not in k and "SECRET" not in k},
}
save_results()

if gpu_info is None:
    record_failure("gpu_detect", "no_gpu", gpu_err)
    save_results()
    raise RuntimeError(
        "No NVIDIA GPU is attached, so nothing was installed or downloaded. In Colab choose "
        "Runtime → Change runtime type → T4 GPU, then Runtime → Run all again.\n" + str(gpu_err))

g = gpu_info["gpus"][0]
warnings = []
if len(gpu_info["gpus"]) > 1:
    warnings.append(f"{len(gpu_info['gpus'])} GPUs found; only GPU 0 is used.")
if "T4" not in g["name"]:
    warnings.append(f"GPU is {g['name']}, not the T4 the specification targets. Results are still recorded.")
if g["memory_total_mib"] < 15000:
    warnings.append(f"GPU has {g['memory_total_mib']} MiB; the plan assumes a 16 GB T4 (~15360 MiB).")
if g["memory_used_mib"] > 500:
    warnings.append(f"{g['memory_used_mib']} MiB of GPU memory is already in use. Restart the session for a clean measurement.")
if RESULTS["environment"]["disk_free_gib"] < 30:
    warnings.append(f"Only {RESULTS['environment']['disk_free_gib']} GiB disk free; ~25 GiB is needed.")
RESULTS["environment"]["warnings"] = warnings
STATUS["gpu_detect"] = "ok"
save_results()

hm = RESULTS["environment"]["host_memory"]
print(f"Run ID        {RUN_ID}")
print(f"GPU           {g['name']} · compute capability {g.get('compute_cap', '?')} · driver {g['driver_version']} · CUDA (driver) {gpu_info['driver_cuda_version']}")
print(f"GPU memory    total {g['memory_total_mib']} MiB · used {g['memory_used_mib']} MiB · free {g['memory_free_mib']} MiB")
print(f"Host RAM      total {hm['total_mib']} MiB · available {hm['available_mib']} MiB")
print(f"Disk free     {RESULTS['environment']['disk_free_gib']} GiB at {BASE}")
print(f"Results       {OUT / 'results.json'}")
for w in warnings:
    print("WARNING:", w)
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 2 · Choose the mode and configuration
# small  (default): Qwen3-1.7B + BGE-M3 + DeBERTa baseline, one document question, ~15–25 minutes.
# target (opt-in):  Qwen3-8B-AWQ + BGE-M3 + DeBERTa baseline, full ten-question benchmark, ~70–100 minutes.
MODE = "small"  # @param ["small", "target"]

CONFIG = {
    "mode": MODE,
    # vLLM runs in its own virtual environment so it cannot disturb Colab's torch/transformers.
    "vllm_version": "0.11.0",
    "vllm_python": "3.12",
    # Revisions are resolved to exact commit SHAs at download time and recorded in results.json.
    "models": {
        "chat_small": {"repo": "Qwen/Qwen3-1.7B", "revision": "main"},
        "chat_target": {"repo": "Qwen/Qwen3-8B-AWQ", "revision": "main"},
        "embed": {"repo": "BAAI/bge-m3", "revision": "main"},
        "checker": {
            "repo": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
            "revision": "main",
            "role": "development baseline: public zero-shot NLI DeBERTa-v3-base checkpoint, NOT the fine-tuned checker",
        },
    },
    "modes": {
        "small": {
            "chat": "chat_small",
            "quantization": None,          # 16-bit weights served as float16
            "utilizations": [0.50],
            "question": "Q05",
            "purpose": "Smoke run: one NileTech document question through embedding, answer generation and checking. "
                       "Not a measurement of the target model; never compare it with target results.",
        },
        "target": {
            "chat": "chat_target",
            "quantization": "awq",
            # Section 6 says 0.85; section 13 asks about 0.75. Both are measured, in this order.
            "utilizations": [0.85, 0.75],
            "purpose": "Target benchmark: Qwen3-8B-AWQ co-resident with BGE-M3 and the checker on the ten timing questions.",
        },
    },
    "vllm": {
        "dtype": "half",            # T4 has no bfloat16
        "max_model_len": 4096,      # specification section 6
        "max_num_seqs": 8,
        "enforce_eager": False,     # set True if CUDA-graph capture fails or runs out of memory
        "attention_backend": None,  # e.g. "TRITON_ATTN" or "FLEX_ATTENTION" if the default backend fails on T4
        "seed": 0,
        "port": 8000,
    },
    "embed_dtype": "float16",
    "checker_dtype": "float16",
    "checker_max_tokens": 512,
    "checker_support_threshold": 0.5,   # uncalibrated development value
    "max_answer_sentences_checked": 8,
    "max_tokens": {"router": 48, "sql": 256, "report": 400, "answer": 400},
    "repeats": 2,                        # target mode: passes over the ten questions per configuration
    "concurrent_requests": 4,
    "concurrent_max_tokens": 256,
    "cancel_after_chunks": 20,
    "startup_timeout_s": 1500,
    "request_timeout_s": 600,
    "save_to_drive": True,
    "drive_dir": "/content/drive/MyDrive/eaios-m1-gpu-feasibility",
}
if MODE not in CONFIG["modes"]:
    raise ValueError(f"MODE must be one of {sorted(CONFIG['modes'])}, not {MODE!r}")
if RESULTS.get("mode") not in (None, MODE) or (RESULTS.get("mode") is None and RESULTS["configs"]):
    raise RuntimeError("This runtime already holds a run in another mode. Use Runtime → Run all so cell 1 starts a new run ID.")
MODE_SPEC = CONFIG["modes"][MODE]
CHAT_KEY = MODE_SPEC["chat"]
MODE_MODELS = [CHAT_KEY, "embed", "checker"]   # only these are downloaded
RESULTS["mode"] = MODE
RESULTS["mode_purpose"] = MODE_SPEC["purpose"]
RESULTS["config"] = CONFIG
save_results()
print(f"MODE = {MODE}: {MODE_SPEC['purpose']}")
print("Models to download:", ", ".join(CONFIG["models"][k]["repo"] for k in MODE_MODELS))
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 3 · Mirror results to Google Drive (optional; survives a disconnected session)
def _mount_drive():
    global DRIVE_DIR
    if not CONFIG["save_to_drive"]:
        return {"mounted": False, "reason": "save_to_drive is False"}
    from google.colab import drive
    drive.mount("/content/drive")
    d = pathlib.Path(CONFIG["drive_dir"]) / MODE / RUN_ID
    d.mkdir(parents=True, exist_ok=True)
    DRIVE_DIR = d
    return {"mounted": True, "dir": str(d)}

run_stage("drive_mount", _mount_drive)
print("Drive mirror:", DRIVE_DIR or "off (results stay in this runtime until you download them)")
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 4 · Install vLLM (separate venv) and small helpers
import importlib.metadata as _md

VLLM_ENV = BASE / "vllm-env"
VLLM_PY = VLLM_ENV / "bin" / "python"
VLLM_CLI = VLLM_ENV / "bin" / "vllm"


def _pkg_version(name):
    try:
        return _md.version(name)
    except _md.PackageNotFoundError:
        return None


def _install():
    details = {}
    # Kernel side: aux models use Colab's preinstalled torch + transformers.
    need = []
    for module, package in [("pynvml", "nvidia-ml-py"), ("sentencepiece", "sentencepiece"),
                            ("google.protobuf", "protobuf"), ("psutil", "psutil"), ("requests", "requests")]:
        try:
            importlib.import_module(module)
        except ImportError:
            need.append(package)
    if need:
        rc, out = sh([sys.executable, "-m", "pip", "install", "-q", *need], timeout=900)
        if rc != 0:
            raise RuntimeError("kernel pip install failed: " + out[-3000:])
    details["kernel_installed"] = need

    # vLLM side
    have = False
    if VLLM_PY.exists():
        rc, out = sh([str(VLLM_PY), "-c", "import importlib.metadata as m; print(m.version('vllm'))"])
        have = rc == 0 and out.strip().splitlines()[-1] == CONFIG["vllm_version"]
    if not have:
        uv = shutil.which("uv")
        if not uv:
            rc, out = sh([sys.executable, "-m", "pip", "install", "-q", "uv"], timeout=600)
            uv = shutil.which("uv") or str(pathlib.Path(sys.executable).parent / "uv")
        t = time.perf_counter()
        if VLLM_ENV.exists():
            shutil.rmtree(VLLM_ENV)
        rc, out = sh([uv, "venv", "--python", CONFIG["vllm_python"], str(VLLM_ENV)], timeout=900)
        if rc != 0:
            raise RuntimeError("uv venv failed: " + out[-3000:])
        rc, out = sh([uv, "pip", "install", "--python", str(VLLM_PY), f"vllm=={CONFIG['vllm_version']}"], timeout=2400)
        (OUT / "install-vllm.log").write_text(out)
        if rc != 0:
            raise RuntimeError("vLLM install failed (see install-vllm.log): " + out[-3000:])
        details["vllm_install_s"] = round(time.perf_counter() - t, 1)
    else:
        details["vllm_install_s"] = 0.0
        details["vllm_reused"] = True

    probe = ("import json, importlib.metadata as m\n"
             "out = {p: None for p in ['vllm','torch','transformers','triton','xformers','flashinfer-python','tokenizers']}\n"
             "for p in out:\n"
             "    try: out[p] = m.version(p)\n"
             "    except Exception: pass\n"
             "import torch, platform\n"
             "out['torch_cuda'] = torch.version.cuda\n"
             "out['python'] = platform.python_version()\n"
             "print(json.dumps(out))")
    rc, out = sh([str(VLLM_PY), "-c", probe], timeout=600)
    if rc != 0:
        raise RuntimeError("could not import torch in the vLLM venv: " + out[-3000:])
    details["vllm_env"] = json.loads(out.strip().splitlines()[-1])
    _, freeze = sh(["uv", "pip", "freeze", "--python", str(VLLM_PY)]) if shutil.which("uv") else sh([str(VLLM_PY), "-m", "pip", "freeze"])
    (OUT / "vllm-env-freeze.txt").write_text(freeze)
    details["kernel_env"] = {p: _pkg_version(p) for p in
                             ["torch", "transformers", "tokenizers", "huggingface-hub", "nvidia-ml-py",
                              "sentencepiece", "protobuf", "accelerate", "safetensors", "requests"]}
    RESULTS["environment"]["vllm_env"] = details["vllm_env"]
    RESULTS["environment"]["kernel_env"] = details["kernel_env"]
    return details

run_stage("install", _install, requires=["gpu_detect"])
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 5 · Download only the selected mode's three models (exact revisions recorded)
MODELS_DIR = BASE / "models"
MODEL_PATHS = {}
EXTRA_FILES = {"embed": ["sparse_linear.pt", "colbert_linear.pt"]}


def _pick_files(files, extra=()):
    root = [f for f in files if "/" not in f]           # skips onnx/ and other subfolders
    weights = [f for f in root if f.endswith(".safetensors")] or \
              [f for f in root if f.endswith(".bin") and not f.startswith("training_args")]
    small = [f for f in root if f.endswith((".json", ".model", ".txt"))]
    return sorted(set(weights + small + [f for f in extra if f in files]))


def _download(key):
    from huggingface_hub import HfApi, snapshot_download
    spec = CONFIG["models"][key]
    info = HfApi().model_info(spec["repo"], revision=spec["revision"], files_metadata=True)
    sizes = {s.rfilename: s.size for s in info.siblings}
    chosen = _pick_files(list(sizes), EXTRA_FILES.get(key, ()))
    need = sum(sizes[f] or 0 for f in chosen)
    free = shutil.disk_usage(str(BASE)).free
    if need > free * 0.9:
        raise RuntimeError(f"not enough disk: need {need/2**30:.1f} GiB, free {free/2**30:.1f} GiB")
    t = time.perf_counter()
    path = snapshot_download(spec["repo"], revision=info.sha, local_dir=str(MODELS_DIR / key), allow_patterns=chosen)
    secs = time.perf_counter() - t
    MODEL_PATHS[key] = pathlib.Path(path)
    rec = {"repo": spec["repo"], "requested_revision": spec["revision"], "resolved_sha": info.sha,
           "files": {f: sizes[f] for f in chosen}, "bytes": need, "download_s": round(secs, 1),
           "license": getattr(info.card_data, "license", None) if info.card_data else None,
           "local_path": str(path)}
    if key == "checker":
        rec["role"] = spec["role"]
    RESULTS["downloads"][key] = rec
    return {k: v for k, v in rec.items() if k != "files"}

for _key in MODE_MODELS:
    run_stage(f"download/{_key}", lambda k=_key: _download(k), requires=["gpu_detect"])
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 6 · The frozen ten-question workload (NileTech fixtures; m1-gpu-workload-v1)
SALES_SCHEMA = """PostgreSQL tables (every query is tenant-scoped by the database; do not filter on tenant_id):
sales_customers(id uuid, name text, region text, country text, since_date date)
sales_products(id uuid, sku text, name text, tier text, unit_price numeric(14,2), currency text, is_active boolean)
sales_orders(id uuid, order_number text, customer_id uuid, order_date date, region text,
             status text CHECK (status IN ('FULFILLED','PENDING','CANCELLED')),
             subtotal numeric(14,2) -- tax-exclusive, tax numeric(14,2), total numeric(14,2) -- tax-inclusive, currency text)
sales_order_lines(id uuid, order_id uuid, product_id uuid, quantity integer, unit_price numeric(14,2), line_total numeric(14,2))
sales_invoices(id uuid, order_id uuid, invoice_number text, issue_date date, due_date date, amount numeric(14,2),
               currency text, status text CHECK (status IN ('PAID','OPEN','OVERDUE')))
Reporting rules: a quarter is by order_date, inclusive; "sales" means FULFILLED orders only; net = subtotal (tax-exclusive),
gross = total; tax is 14%; currency USD; invoice payment status does not affect sales."""

PASSAGES = {
    "leave-entitlement": "NileTech Solutions — Leave Policy (version 2026.1). Annual Leave Entitlement: Egypt (EG): 21 days per year for full-time staff. United Arab Emirates (AE): 22 days per year for full-time staff. Part-time staff accrue 10 days (EG) and 11 days (AE), pro-rated. Contractors do not accrue paid annual leave.",
    "leave-conditions": "NileTech Solutions — Leave Policy (version 2026.1). Conditions: Leave accrues monthly after a 3-month probation period. Up to 5 unused days may be carried into the next year. All leave requires manager approval before it is taken.",
    "remote-work": "NileTech Solutions — Remote Work Policy (version 2026.1). Stated values: Core Hours: 10:00-16:00. Max Remote Days Per Week: 3.",
    "expense": "NileTech Solutions — Expense Policy (version 2026.1), owner Salma ElGendy, Finance Lead. Stated values: Approval Threshold: 500. Currency: USD. Receipt Required Above: 25.",
    "travel": "NileTech Solutions — Travel Policy (version 2026.1). Stated values: Advance Booking Days: 14. Economy Only Below Hours: 6.",
    "benefits": "NileTech Solutions — Benefits Guide (version 2026.1). Stated values: Currency: USD. Health Cover Percent: 80. Training Budget: 1500.",
    "security": "NileTech Solutions — Information Security Policy (version 2026.1). Stated values: MFA Required: True. Password Min Length: 14. Session Timeout Minutes: 30.",
    "contract-helios": "Customer Agreement — Helios Logistics Group. Governing law: Arab Republic of Egypt. Termination: Either party may terminate this agreement on 30 days' written notice. Limitation of Liability: Aggregate liability is capped at 50000.00 USD. Payment Terms: Invoices are payable NET_30 from the date of issue.",
    "contract-zenith": "Customer Agreement — Zenith Manufacturing Ltd. Governing law: Arab Republic of Egypt. Termination: Either party may terminate this agreement on 90 days' written notice. Limitation of Liability: Liability under this agreement is uncapped. Payment Terms: Invoices are payable NET_30 from the date of issue.",
    "contract-allen-mason": "Customer — Allen-Mason. Governing law: Arab Republic of Egypt. Termination: Either party may terminate on 60 days' written notice. Limitation of Liability: Liability under this agreement is uncapped. Payment Terms: Invoices are payable NET_45 from issue.",
}

ROWS_Q1_FULFILLED = "Query result (dataset niletech-0.1.6, Q1 2026 = order_date 2026-01-01..2026-03-31, status FULFILLED): fulfilled_orders=291; net_sales_usd=80934385.00; tax_usd=11330813.90; gross_sales_usd=92265198.90."
ROWS_Q1_BY_STATUS = "Query result (dataset niletech-0.1.6, Q1 2026 by order status, net = tax-exclusive subtotal, USD): FULFILLED orders=291 net=80934385.00; PENDING orders=40 net=12605562.00; CANCELLED orders=28 net=9447839.00; all statuses orders=359 net=102987786.00."
ROWS_SUBSET_EXCLUDED = "Query result (worked subset, orders excluded from Q1 2026 fulfilled sales): SO-001667 order_date=2025-12-31 status=FULFILLED subtotal=288392.00 reason=before quarter; SO-001755 order_date=2025-12-31 status=FULFILLED subtotal=327066.00 invoice_issue_date=2026-01-02 reason=before quarter (order date basis); SO-001807 order_date=2026-01-02 status=PENDING subtotal=17520.00 reason=not fulfilled; SO-001818 order_date=2026-01-12 status=CANCELLED subtotal=41373.00 reason=cancelled; SO-000268 order_date=2026-04-01 status=FULFILLED subtotal=286109.00 reason=after quarter."

QUESTIONS = [
    {"id": "Q01", "route": "database", "question": "What were NileTech's net (tax-exclusive) fulfilled sales for Q1 2026, in USD?",
     "evidence": ROWS_Q1_FULFILLED,
     "claims": [("Net fulfilled sales in Q1 2026 were USD 80,934,385.00.", True),
                ("Net fulfilled sales in Q1 2026 were USD 102,987,786.00.", False)]},
    {"id": "Q02", "route": "database", "question": "Make last quarter's sales report for Q1 2026, broken down by fulfilled, pending and cancelled orders.",
     "evidence": ROWS_Q1_BY_STATUS,
     "claims": [("Cancelled orders had a net value of USD 9,447,839.00 in Q1 2026.", True),
                ("Pending orders had a net value of USD 9,447,839.00 in Q1 2026.", False)]},
    {"id": "Q03", "route": "database", "question": "What were the gross (tax-inclusive) fulfilled sales and the tax amount for Q1 2026?",
     "evidence": ROWS_Q1_FULFILLED,
     "claims": [("Gross fulfilled sales in Q1 2026 were USD 92,265,198.90.", True),
                ("Tax on fulfilled sales in Q1 2026 was USD 12,605,562.00.", False)]},
    {"id": "Q04", "route": "database", "question": "Which orders near the Q1 2026 boundaries were excluded from fulfilled sales, and why?",
     "evidence": ROWS_SUBSET_EXCLUDED,
     "claims": [("Order SO-001818 was excluded because it was cancelled.", True),
                ("Order SO-001818 was excluded because it was ordered after the quarter.", False)]},
    {"id": "Q05", "route": "knowledge", "question": "How many days of annual leave do full-time employees in Egypt get?",
     "passages": ["leave-entitlement", "leave-conditions"],
     "claims": [("Full-time staff in Egypt receive 21 days of annual leave per year.", True),
                ("Full-time staff in Egypt receive 22 days of annual leave per year.", False)]},
    {"id": "Q06", "route": "knowledge", "question": "How many unused leave days can I carry over into next year?",
     "passages": ["leave-conditions", "leave-entitlement"],
     "claims": [("Up to 5 unused leave days may be carried into the next year.", True),
                ("Up to 10 unused leave days may be carried into the next year.", False)]},
    {"id": "Q07", "route": "knowledge", "question": "How many days a week can I work remotely, and what are the core hours?",
     "passages": ["remote-work"],
     "claims": [("Employees may work remotely up to 3 days per week.", True),
                ("Core hours are 09:00 to 17:00.", False)]},
    {"id": "Q08", "route": "knowledge", "question": "Above what amount do I need a receipt for an expense claim?",
     "passages": ["expense", "travel"],
     "claims": [("A receipt is required for expenses above 25 USD.", True),
                ("A receipt is required for expenses above 500 USD.", False)]},
    {"id": "Q09", "route": "legal", "question": "Compare the termination notice and liability cap in the Helios Logistics and Zenith Manufacturing agreements.",
     "passages": ["contract-helios", "contract-zenith"],
     "claims": [("The Helios Logistics agreement caps aggregate liability at 50000.00 USD.", True),
                ("The Zenith Manufacturing agreement caps liability at 50000.00 USD.", False)]},
    {"id": "Q10", "route": "legal", "question": "What are the payment terms and termination notice period in the Allen-Mason contract?",
     "passages": ["contract-allen-mason"],
     "claims": [("Allen-Mason invoices are payable NET_45 from issue.", True),
                ("Either party may terminate the Allen-Mason contract on 30 days' written notice.", False)]},
]

PROMPTS = {
    "router_system": ("You route requests for an enterprise assistant. Reply with only a JSON object "
                      "{\"route\": \"database\" | \"knowledge\" | \"legal\"}. database = figures from the sales orders and invoices "
                      "database; knowledge = company policies, handbook and HR rules; legal = contracts and agreements."),
    "sql_system": "You write one read-only PostgreSQL SELECT statement that answers the question. Reply with SQL only.\n" + SALES_SCHEMA,
    "report_system": ("You are the report agent. Write a concise report (about 250 words) for a finance manager using ONLY the "
                      "figures in the query result. Copy numbers exactly; do not compute new totals or invent figures."),
    "answer_system": ("Answer the question using ONLY the numbered sources. Cite sources like [1]. If the sources do not contain "
                      "the answer, say you cannot find it. Keep the answer under 200 words."),
    "long_generation": "Write a detailed, multi-section onboarding guide for a new NileTech finance analyst.",
}

WORKLOAD = {"version": "m1-gpu-workload-v1", "dataset": "niletech-0.1.6",
            "note": "Frozen timing workload, accepted by the owner on 2026-11-20 for repeatable latency measurement; kept separate from held-out quality evaluation. The specification does not enumerate its ten questions.",
            "passages": PASSAGES, "questions": QUESTIONS, "prompts": PROMPTS}
_canonical = json.dumps(WORKLOAD, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
WORKLOAD_SHA256 = hashlib.sha256(_canonical).hexdigest()
RESULTS["workload"] = {"version": WORKLOAD["version"], "sha256": WORKLOAD_SHA256, "dataset": WORKLOAD["dataset"],
                       "note": WORKLOAD["note"],
                       "questions": [{"id": q["id"], "route": q["route"], "question": q["question"]} for q in QUESTIONS]}
(OUT / "workload.json").write_text(json.dumps(WORKLOAD, indent=2, ensure_ascii=False))
STATUS["workload"] = "ok"
save_results()
print(f"{len(QUESTIONS)} questions · workload sha256 {WORKLOAD_SHA256}")
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 7 · Benchmark helpers (memory sampler, vLLM server, chat client, BGE-M3, checker)
# ---- bench_core.py (inlined verbatim by build_notebook.py; tested on CPU in tests/) ----
# <<BENCH_CORE>>
# ---- end bench_core.py ----
import gc, secrets, signal, statistics, threading
import requests

# ---- GPU / host memory sampling ------------------------------------------------
class MemorySampler:
    """Polls NVML every `interval` seconds. Open windows record their own peaks."""

    def __init__(self, interval=0.1):
        import pynvml
        self.nv = pynvml
        pynvml.nvmlInit()
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.total_mib = pynvml.nvmlDeviceGetMemoryInfo(self.handle).total // 2**20
        self.interval = interval
        self.windows = {}
        self.lock = threading.Lock()
        self.stop_flag = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def used_mib(self):
        return self.nv.nvmlDeviceGetMemoryInfo(self.handle).used // 2**20

    def _procs(self):
        try:
            return {p.pid: (p.usedGpuMemory or 0) // 2**20 for p in self.nv.nvmlDeviceGetComputeRunningProcesses(self.handle)}
        except Exception:
            return {}

    def _loop(self):
        import psutil
        while not self.stop_flag:
            try:
                used = self.used_mib()
                procs = self._procs()
                ram = psutil.virtual_memory().used // 2**20
                kt = _torch_now()
                with self.lock:
                    for w in self.windows.values():
                        w["samples"] += 1
                        w["peak_device_used_mib"] = max(w["peak_device_used_mib"], used)
                        w["peak_host_ram_used_mib"] = max(w["peak_host_ram_used_mib"], ram)
                        if kt:
                            w["kernel_torch_peak_allocated_mib"] = max(w["kernel_torch_peak_allocated_mib"], kt[0])
                            w["kernel_torch_peak_reserved_mib"] = max(w["kernel_torch_peak_reserved_mib"], kt[1])
                        for pid, mib in procs.items():
                            w["peak_by_pid_mib"][pid] = max(w["peak_by_pid_mib"].get(pid, 0), mib)
            except Exception:
                pass
            time.sleep(self.interval)

    def open(self, name):
        with self.lock:
            self.windows[name] = {"start_device_used_mib": self.used_mib(), "peak_device_used_mib": 0,
                                  "peak_host_ram_used_mib": 0, "peak_by_pid_mib": {}, "samples": 0,
                                  "kernel_torch_peak_allocated_mib": 0, "kernel_torch_peak_reserved_mib": 0}

    def close(self, name):
        time.sleep(self.interval * 2)
        with self.lock:
            w = self.windows.pop(name)
        w["end_device_used_mib"] = self.used_mib()
        w["device_total_mib"] = self.total_mib
        w["min_headroom_mib"] = self.total_mib - w["peak_device_used_mib"]
        w["peak_by_pid_mib"] = {str(k): v for k, v in w["peak_by_pid_mib"].items()}
        return w


def _torch_ready():
    return "torch" in sys.modules and sys.modules["torch"].cuda.is_initialized()


def _torch_now():
    """(allocated, reserved) MiB of this kernel's torch allocator, or None before CUDA is initialised."""
    if not _torch_ready():
        return None
    t = sys.modules["torch"]
    return t.cuda.memory_allocated() // 2**20, t.cuda.memory_reserved() // 2**20


def _torch_peaks():
    if not _torch_ready():
        return {}
    t = sys.modules["torch"]
    return {"kernel_torch_max_allocated_since_start_mib": t.cuda.max_memory_allocated() // 2**20,
            "kernel_torch_reserved_now_mib": t.cuda.memory_reserved() // 2**20}


SAMPLER = MemorySampler()


class MemWindow:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink
    def __enter__(self):
        SAMPLER.open(self.name)
        return self
    def __exit__(self, *exc):
        self.sink["memory"] = SAMPLER.close(self.name)
        return False


# ---- vLLM server ----------------------------------------------------------------
SERVED_NAME = CONFIG["models"][CHAT_KEY]["repo"]
API_KEY = secrets.token_urlsafe(24)  # service-to-service auth even on localhost; never saved
BASE_URL = f"http://127.0.0.1:{CONFIG['vllm']['port']}"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

LOG_FACTS = {
    "attention_backend": r"(Using [A-Za-z0-9_ ]*(?:backend|Backend)[^\n]*)",
    "quantization": r"((?:awq|AWQ)[^\n]{0,160})",
    "model_load": r"(Model loading took[^\n]*)",
    "kv_cache_memory": r"(Available KV cache memory[^\n]*)",
    "kv_cache_size": r"(GPU KV cache size[^\n]*)",
    "max_concurrency": r"(Maximum concurrency[^\n]*)",
    "cuda_graphs": r"(Graph capturing finished[^\n]*|CUDA graph[^\n]*)",
    "init_engine": r"(init engine[^\n]*took[^\n]*)",
    "memory_profile": r"(Memory profiling takes[^\n]*|memory profiling[^\n]*)",
    "errors": r"((?:ERROR|Error|error)[^\n]*)",
}


class VllmServer:
    def __init__(self, util, label):
        self.util, self.label = util, label
        self.proc = None
        self.log_path = OUT / f"vllm-{label}.log"

    def command(self):
        v = CONFIG["vllm"]
        cmd = [str(VLLM_CLI), "serve", str(MODEL_PATHS[CHAT_KEY]),
               "--served-model-name", SERVED_NAME, "--host", "127.0.0.1", "--port", str(v["port"]),
               "--dtype", v["dtype"],
               *(["--quantization", MODE_SPEC["quantization"]] if MODE_SPEC["quantization"] else []),
               "--max-model-len", str(v["max_model_len"]), "--max-num-seqs", str(v["max_num_seqs"]),
               "--gpu-memory-utilization", str(self.util), "--seed", str(v["seed"]), "--api-key", API_KEY]
        if v["enforce_eager"]:
            cmd.append("--enforce-eager")
        return cmd

    def start(self):
        env = dict(os.environ, HF_HUB_OFFLINE="1", VLLM_NO_USAGE_STATS="1", DO_NOT_TRACK="1",
                   PATH=f"{VLLM_ENV / 'bin'}:{os.environ.get('PATH', '')}")
        env.pop("VIRTUAL_ENV", None)
        if CONFIG["vllm"]["attention_backend"]:
            env["VLLM_ATTENTION_BACKEND"] = CONFIG["vllm"]["attention_backend"]
        free_before = SAMPLER.total_mib - SAMPLER.used_mib()
        self.log_file = open(self.log_path, "w")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(self.command(), stdout=self.log_file, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
        deadline = t0 + CONFIG["startup_timeout_s"]
        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"vLLM exited with code {self.proc.returncode} during startup. Log tail:\n{self.tail(60)}")
            try:
                if requests.get(BASE_URL + "/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(2)
        else:
            self.stop()
            raise TimeoutError(f"vLLM not ready after {CONFIG['startup_timeout_s']} s. Log tail:\n{self.tail(60)}")
        ready_s = time.perf_counter() - t0
        models = requests.get(BASE_URL + "/v1/models", headers=HEADERS, timeout=10).json()
        return {"utilization": self.util, "ready_s": round(ready_s, 1), "pid": self.proc.pid,
                "free_mib_before_start": free_before, "device_used_mib_after_ready": SAMPLER.used_mib(),
                "served_models": [m.get("id") for m in models.get("data", [])],
                "max_model_len": (models.get("data") or [{}])[0].get("max_model_len"),
                "command": [c if c != API_KEY else "<redacted>" for c in self.command()],
                "log_facts": self.facts()}

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=90):
        if self.proc is None:
            return {"stopped": True, "note": "not running"}
        t0 = time.perf_counter()
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
                self.proc.wait(timeout=timeout)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                self.proc.wait(timeout=30)
        stop_s = time.perf_counter() - t0
        try:
            self.log_file.close()
        except Exception:
            pass
        # wait until the GPU memory is actually released
        t1 = time.perf_counter()
        baseline = (_torch_now() or (0, 0))[1] + 1200  # kernel allocator + CUDA contexts
        while SAMPLER.used_mib() > baseline and time.perf_counter() - t1 < 60:
            time.sleep(1)
        return {"stop_s": round(stop_s, 1), "release_wait_s": round(time.perf_counter() - t1, 1),
                "device_used_mib_after_stop": SAMPLER.used_mib(), "exit_code": self.proc.returncode}

    def tail(self, n=40):
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    def facts(self):
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return {}
        out = {}
        for key, pattern in LOG_FACTS.items():
            found = re.findall(pattern, text)
            if found:
                out[key] = [f.strip()[:300] for f in found[:5]] if key == "errors" else found[-1].strip()[:300]
        out["oom_in_log"] = bool(re.search(r"out of memory|CUDA error|OutOfMemoryError", text, re.I))
        return out


def server_metric(name):
    """Sum a Prometheus gauge from vLLM's /metrics endpoint."""
    text = requests.get(BASE_URL + "/metrics", headers=HEADERS, timeout=5).text
    total, found = 0.0, False
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            total += float(line.rsplit(" ", 1)[-1])
            found = True
    return total if found else None


# ---- chat client ----------------------------------------------------------------
class ModelCallError(RuntimeError):
    pass


def chat(messages, max_tokens, stop_after_chunks=None, timeout=None):
    """Stream one chat completion. With `stop_after_chunks`, the client closes the connection after
    that many content chunks if the generation is still unfinished (see consume_sse)."""
    body = {"model": SERVED_NAME, "messages": messages, "max_tokens": max_tokens, "temperature": 0.0,
            "seed": CONFIG["vllm"]["seed"], "stream": True, "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False}}
    t0 = time.perf_counter()
    with requests.post(BASE_URL + "/v1/chat/completions", json=body, headers=HEADERS, stream=True,
                       timeout=(10, timeout or CONFIG["request_timeout_s"])) as r:
        if r.status_code != 200:
            raise ModelCallError(f"HTTP {r.status_code}: {r.text[:1500]}")
        res = consume_sse(r.iter_lines(decode_unicode=True), stop_after_chunks, t0=t0)
    # Leaving the `with` closes the connection; after an early stop that abandons the generation.
    usage, ttft, total = res["usage"], res["ttft_s"], res["total_s"]
    text = re.sub(r"<think>.*?</think>", "", res["text_raw"], flags=re.S).strip()
    ctoks = usage.get("completion_tokens")
    decode = (ctoks - 1) / (total - ttft) if ctoks and ttft and total > ttft and ctoks > 1 else None
    return {"text": text, "ttft_s": round(ttft, 3) if ttft else None, "total_s": round(total, 3),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": ctoks,
            "decode_tok_per_s": round(decode, 2) if decode else None, "finish_reason": res["finish_reason"],
            "chunks": res["chunks"], "stream_done": res["stream_done"], "client_closed_early": res["client_closed_early"]}


# ---- BGE-M3 (dense + sparse heads, as in FlagEmbedding's BGEM3FlagModel) ----------
AUX = {}


class BgeM3:
    def __init__(self, path, dtype):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModel.from_pretrained(path, torch_dtype=getattr(torch, dtype)).to("cuda").eval()
        hidden = self.model.config.hidden_size
        self.sparse = torch.nn.Linear(hidden, 1).to("cuda", getattr(torch, dtype))
        self.sparse.load_state_dict(torch.load(pathlib.Path(path) / "sparse_linear.pt", map_location="cuda", weights_only=True))
        self.skip_ids = {i for i in (self.tok.cls_token_id, self.tok.eos_token_id, self.tok.pad_token_id, self.tok.unk_token_id) if i is not None}
        self.max_len = 8192

    def encode(self, texts):
        torch = self.torch
        with torch.inference_mode():
            batch = self.tok(texts, padding=True, truncation=False, return_tensors="pt")
            n = int(batch["attention_mask"].sum(1).max())
            if n > self.max_len:
                raise ValueError(f"input has {n} tokens > {self.max_len}; refusing to truncate")
            batch = {k: v.to("cuda") for k, v in batch.items()}
            hidden = self.model(**batch).last_hidden_state
            dense = torch.nn.functional.normalize(hidden[:, 0].float(), dim=-1)
            weights = torch.relu(self.sparse(hidden)).squeeze(-1).float()
            torch.cuda.synchronize()
        ids, w, mask = batch["input_ids"].cpu().tolist(), weights.cpu().tolist(), batch["attention_mask"].cpu().tolist()
        sparse = []
        for row_ids, row_w, row_m in zip(ids, w, mask):
            d = {}
            for t, wt, m in zip(row_ids, row_w, row_m):
                if m and t not in self.skip_ids and wt > 0:
                    d[t] = max(d.get(t, 0.0), wt)
            sparse.append(d)
        return dense.cpu(), sparse, n


def lexical(a, b):
    return sum(w * b[t] for t, w in a.items() if t in b)


# ---- DeBERTa NLI checker ---------------------------------------------------------
class Checker:
    def __init__(self, path, dtype):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        try:
            self.tok = AutoTokenizer.from_pretrained(path)
        except Exception:
            self.tok = AutoTokenizer.from_pretrained(path, use_fast=False)
        self.model = AutoModelForSequenceClassification.from_pretrained(path, torch_dtype=getattr(torch, dtype)).to("cuda").eval()
        self.labels = {int(k): v.lower() for k, v in self.model.config.id2label.items()}
        self.entail = next(i for i, l in self.labels.items() if l.startswith("entail"))

    def score(self, premise, claim):
        torch = self.torch
        enc = self.tok(premise, claim, truncation=False, return_tensors="pt")
        n = enc["input_ids"].shape[1]
        if n > CONFIG["checker_max_tokens"]:
            return {"status": "over_token_limit", "tokens": n}
        t0 = time.perf_counter()
        with torch.inference_mode():
            logits = self.model(**{k: v.to("cuda") for k, v in enc.items()}).logits.float()
            probs = torch.softmax(logits, -1)[0].cpu().tolist()
            torch.cuda.synchronize()
        if any(p != p for p in probs):
            return {"status": "nan_output", "tokens": n}
        p = probs[self.entail]
        return {"status": "ok", "tokens": n, "latency_s": round(time.perf_counter() - t0, 4),
                "entailment": round(p, 4), "supported": p >= CONFIG["checker_support_threshold"],
                "probs": {self.labels[i]: round(v, 4) for i, v in enumerate(probs)}}


def load_aux():
    import torch
    out = {"torch": torch.__version__, "torch_cuda": torch.version.cuda}
    t = time.perf_counter()
    AUX["bge"] = BgeM3(MODEL_PATHS["embed"], CONFIG["embed_dtype"])
    out["bge_load_s"] = round(time.perf_counter() - t, 2)
    out["bge_mib_after"] = SAMPLER.used_mib()
    t = time.perf_counter()
    AUX["checker"] = Checker(MODEL_PATHS["checker"], CONFIG["checker_dtype"])
    out["checker_load_s"] = round(time.perf_counter() - t, 2)
    out["checker_labels"] = AUX["checker"].labels
    out["device_used_mib_after"] = SAMPLER.used_mib()
    out.update(_torch_peaks())
    return out


def unload_aux():
    AUX.clear()
    gc.collect()
    if _torch_ready():
        sys.modules["torch"].cuda.empty_cache()
    time.sleep(2)
    return {"device_used_mib_after_unload": SAMPLER.used_mib()}


# ---- one question through the pipeline -------------------------------------------
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[])")


def evidence_for(q):
    if q["route"] == "database":
        return q["evidence"]
    return "\n".join(f"[{i}] {PASSAGES[p]}" for i, p in enumerate(q["passages"], 1))


def parse_route(text):
    m = re.search(r"\{.*?\}", text, re.S)
    try:
        return json.loads(m.group(0)).get("route") if m else None
    except Exception:
        return None


def run_question(q, corpus, use_router=True):
    rec = {"id": q["id"], "route_expected": q["route"], "steps": {}, "errors": []}
    t_all = time.perf_counter()
    # 1. router (target mode only; small mode measures embedding → answer → checking)
    if use_router:
        r = chat([{"role": "system", "content": PROMPTS["router_system"]}, {"role": "user", "content": q["question"]}],
                 CONFIG["max_tokens"]["router"])
        rec["steps"]["router"] = {k: r[k] for k in ("total_s", "ttft_s", "prompt_tokens", "completion_tokens")}
        rec["route_predicted"] = parse_route(r["text"])
        rec["route_match"] = rec["route_predicted"] == q["route"]
    # 2. query embedding (+ smoke ranking against the small passage corpus)
    t = time.perf_counter()
    qd, qs, qn = AUX["bge"].encode([q["question"]])
    emb_s = time.perf_counter() - t
    dense_scores = (corpus["dense"] @ qd[0]).tolist()
    hybrid = {pid: dense_scores[i] + 0.3 * lexical(qs[0], corpus["sparse"][i]) for i, pid in enumerate(corpus["ids"])}
    top = sorted(hybrid, key=hybrid.get, reverse=True)[:3]
    rec["steps"]["embed_query"] = {"total_s": round(emb_s, 4), "tokens": qn}
    if q["route"] != "database":
        rec["retrieval_top3"] = top
        rec["retrieval_hit_at_1"] = top[0] == q["passages"][0]
    # 3. generation
    evidence = evidence_for(q)
    if q["route"] == "database":
        s = chat([{"role": "system", "content": PROMPTS["sql_system"]}, {"role": "user", "content": q["question"]}],
                 CONFIG["max_tokens"]["sql"])
        rec["steps"]["sql"] = {k: s[k] for k in ("total_s", "ttft_s", "prompt_tokens", "completion_tokens", "decode_tok_per_s", "finish_reason")}
        rec["sql_text"] = s["text"][:2000]
        rec["sql_smoke"] = {"has_select": "select" in s["text"].lower(), "mentions_fulfilled": "FULFILLED" in s["text"].upper()}
        g = chat([{"role": "system", "content": PROMPTS["report_system"]},
                  {"role": "user", "content": f"Question: {q['question']}\n\n{evidence}"}], CONFIG["max_tokens"]["report"])
        rec["steps"]["report"] = {k: g[k] for k in ("total_s", "ttft_s", "prompt_tokens", "completion_tokens", "decode_tok_per_s", "finish_reason")}
    else:
        g = chat([{"role": "system", "content": PROMPTS["answer_system"]},
                  {"role": "user", "content": f"Sources:\n{evidence}\n\nQuestion: {q['question']}"}], CONFIG["max_tokens"]["answer"])
        rec["steps"]["answer"] = {k: g[k] for k in ("total_s", "ttft_s", "prompt_tokens", "completion_tokens", "decode_tok_per_s", "finish_reason")}
    rec["answer_text"] = g["text"][:4000]
    # 4. checker: the fixed claims (expected labels) and the generated answer's sentences
    t = time.perf_counter()
    fixed = []
    for claim, expected in q["claims"]:
        sc = AUX["checker"].score(evidence, claim)
        sc.update(claim=claim, expected_supported=expected,
                  agrees=(sc.get("supported") == expected) if sc["status"] == "ok" else None)
        fixed.append(sc)
    sentences = [s.strip() for s in SENT_SPLIT.split(g["text"]) if len(s.strip()) > 3][:CONFIG["max_answer_sentences_checked"]]
    answer_scores = []
    for sent in sentences:
        sc = AUX["checker"].score(evidence, sent)
        sc["sentence"] = sent[:300]
        answer_scores.append(sc)
    rec["steps"]["check"] = {"total_s": round(time.perf_counter() - t, 4), "items": len(fixed) + len(answer_scores),
                             "over_token_limit": sum(1 for s in fixed + answer_scores if s["status"] == "over_token_limit")}
    rec["check_fixed_claims"] = fixed
    rec["check_answer_sentences"] = answer_scores
    rec["pipeline_s"] = round(time.perf_counter() - t_all, 3)
    return rec


print(f"Sampler ready · GPU total {SAMPLER.total_mib} MiB · used now {SAMPLER.used_mib()} MiB")
STATUS["helpers"] = "ok"
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 8 · Run the selected mode (each stage is saved as soon as it finishes)
from concurrent.futures import ThreadPoolExecutor

READY = ["install", *[f"download/{k}" for k in MODE_MODELS], "workload", "helpers"]


def run_config(util, idx):
    key = f"{MODE}/u{util:.2f}"
    cfg = RESULTS["configs"].setdefault(key, {"mode": MODE, "utilization": util, "stages": [], "questions": []})
    st = cfg["stages"]
    p = lambda n: f"{key}/{n}"
    label = key.replace("/", "-")
    srv = VllmServer(util, f"{label}-start1")
    SAMPLER.open(key + "/overall")

    def start():
        d = {}
        with MemWindow(p("vllm_start"), d):
            d.update(srv.start())
        return d
    run_stage(p("vllm_start"), start, requires=READY, container=st)

    def aux():
        d = {}
        with MemWindow(p("aux_load"), d):
            d.update(load_aux())
        return d
    run_stage(p("aux_load"), aux, requires=[p("vllm_start")], container=st)

    def warmup():  # excluded from statistics
        d = {}
        with MemWindow(p("warmup"), d):
            d["chat"] = {k: v for k, v in chat([{"role": "user", "content": "Say ready."}], 8).items() if k != "text"}
            AUX["bge"].encode(["warm-up"])
            AUX["checker"].score("NileTech has offices in Egypt.", "NileTech has an office in Egypt.")
        return d
    run_stage(p("warmup"), warmup, requires=[p("aux_load")], container=st)

    corpus = {}
    def embed_corpus():
        ids = list(PASSAGES)
        t = time.perf_counter()
        dense, sparse, n = AUX["bge"].encode([PASSAGES[i] for i in ids])
        corpus.update(ids=ids, dense=dense, sparse=sparse)
        return {"passages": len(ids), "batch_s": round(time.perf_counter() - t, 4), "max_tokens": n}
    run_stage(p("embed_corpus"), embed_corpus, requires=[p("warmup")], container=st)

    if MODE == "small":
        def document_question():
            q = next(q for q in QUESTIONS if q["id"] == MODE_SPEC["question"])
            d = {}
            with MemWindow(p("document_question"), d):
                rec = run_question(q, corpus, use_router=False)
            rec["rep"] = 1
            cfg["questions"].append(rec)
            d.update(id=q["id"], pipeline_s=rec["pipeline_s"], steps=rec["steps"])
            print(f"   {q['id']}: {rec['pipeline_s']:.1f}s  embed {rec['steps']['embed_query']['total_s']:.3f}s · "
                  f"answer {rec['steps']['answer']['total_s']:.1f}s · check {rec['steps']['check']['total_s']:.2f}s")
            return d
        run_stage(p("document_question"), document_question, requires=[p("embed_corpus")], container=st)
    else:
        for rep in range(1, CONFIG["repeats"] + 1):
            def questions(rep=rep):
                d = {"rep": rep, "ok": 0, "failed": 0}
                with MemWindow(p(f"questions_rep{rep}"), d):
                    for q in QUESTIONS:
                        if not srv.alive():
                            raise RuntimeError("vLLM process died. Log tail:\n" + srv.tail(40))
                        try:
                            rec = run_question(q, corpus)
                            rec["rep"] = rep
                            d["ok"] += 1
                        except Exception as exc:
                            kind = "oom" if "out of memory" in str(exc).lower() else "error"
                            rec = {"id": q["id"], "rep": rep, "error": f"{type(exc).__name__}: {exc}"[:3000],
                                   "traceback": traceback.format_exc()[-3000:]}
                            record_failure(p(f"questions_rep{rep}/{q['id']}"), kind, rec["error"])
                            d["failed"] += 1
                        cfg["questions"].append(rec)
                        print(f"   {q['id']} rep{rep}: " + (f"{rec['pipeline_s']:.1f}s" if "pipeline_s" in rec else "FAILED " + rec["error"][:120]))
                        save_results()
                if d["failed"]:
                    raise RuntimeError(f"{d['failed']} of {len(QUESTIONS)} questions failed")
                return d
            run_stage(p(f"questions_rep{rep}"), questions, requires=[p("embed_corpus")], container=st)

        def concurrent():
            d = {"requests": CONFIG["concurrent_requests"]}
            stop = threading.Event()
            aux_stats = {"ops": 0, "errors": []}
            def aux_loop():
                while not stop.is_set():
                    try:
                        AUX["bge"].encode([QUESTIONS[aux_stats["ops"] % len(QUESTIONS)]["question"]])
                        AUX["checker"].score(PASSAGES["leave-entitlement"], "Egypt staff get 21 days.")
                        aux_stats["ops"] += 1
                    except Exception as exc:
                        aux_stats["errors"].append(f"{type(exc).__name__}: {exc}"[:500])
                        time.sleep(0.5)
            def one(i):
                q = QUESTIONS[i % len(QUESTIONS)]
                try:
                    return chat([{"role": "system", "content": PROMPTS["answer_system"]},
                                 {"role": "user", "content": f"Sources:\n{evidence_for(q)}\n\nQuestion: {q['question']}"}],
                                CONFIG["concurrent_max_tokens"])
                except Exception as exc:
                    return {"error": f"{type(exc).__name__}: {exc}"[:500]}
            with MemWindow(p("concurrent"), d):
                th = threading.Thread(target=aux_loop, daemon=True)
                th.start()
                t = time.perf_counter()
                with ThreadPoolExecutor(CONFIG["concurrent_requests"]) as ex:
                    res = list(ex.map(one, range(CONFIG["concurrent_requests"])))
                wall = time.perf_counter() - t
                stop.set()
                th.join(timeout=30)
            ok = [r for r in res if "error" not in r]
            d.update(wall_s=round(wall, 2), ok=len(ok), errors=[r["error"] for r in res if "error" in r],
                     per_request_s=[r["total_s"] for r in ok], ttft_s=[r["ttft_s"] for r in ok],
                     aggregate_tok_per_s=round(sum(r["completion_tokens"] or 0 for r in ok) / wall, 2),
                     aux_ops=aux_stats["ops"], aux_errors=aux_stats["errors"][:10], server_alive=srv.alive())
            if d["errors"] or aux_stats["errors"] or not srv.alive():
                raise RuntimeError(f"concurrent load had failures: chat={d['errors'][:3]} aux={aux_stats['errors'][:3]} alive={srv.alive()}")
            return d
        run_stage(p("concurrent"), concurrent, requires=[p("embed_corpus")], container=st)

        def cancellation():
            d = {}
            t = time.perf_counter()
            part = chat([{"role": "user", "content": PROMPTS["long_generation"]}], 1500,
                        stop_after_chunks=CONFIG["cancel_after_chunks"])
            d["closed_after_s"] = round(time.perf_counter() - t, 2)
            d["stream"] = {k: part[k] for k in ("chunks", "finish_reason", "stream_done", "client_closed_early", "completion_tokens")}
            t = time.perf_counter()
            running = None
            while time.perf_counter() - t < 30:
                running = server_metric("vllm:num_requests_running")
                if running == 0:
                    break
                time.sleep(0.25)
            d["requests_running_after"] = running
            d["release_s"] = round(time.perf_counter() - t, 2)
            after = chat([{"role": "user", "content": "Reply with the word ok."}], 8)
            d["follow_up_ok"] = bool(after["text"])
            d["server_alive"] = srv.alive()
            d["verdict"] = cancellation_verdict(part, running, d["follow_up_ok"], d["server_alive"])
            cfg["cancellation_evidence"] = d   # kept even when the stage fails
            if not d["verdict"]["ok"]:
                raise RuntimeError("cancellation not demonstrated: " + "; ".join(d["verdict"]["reasons"]))
            return d
        run_stage(p("cancellation"), cancellation, requires=[p("aux_load")], container=st)

        # Restart runs for every target configuration: a configuration without a successful
        # restart is never recommended (it is reported as failed or incomplete).
        def restart():
            nonlocal srv
            d = {"stop": srv.stop()}
            # (a) restart while BGE-M3 + checker stay resident in this kernel — the realistic recovery path
            srv = VllmServer(util, f"{label}-restart-aux-resident")
            try:
                with MemWindow(p("restart_a"), d.setdefault("aux_resident", {})):
                    d["aux_resident"].update(srv.start())
                d["aux_resident"]["ok"] = True
            except Exception as exc:
                d["aux_resident"].update(ok=False, error=f"{type(exc).__name__}: {exc}"[:2000])
                srv.stop()
                # (b) unload aux, restart vLLM, reload aux
                d["unload"] = unload_aux()
                srv = VllmServer(util, f"{label}-restart-aux-reloaded")
                with MemWindow(p("restart_b"), d.setdefault("aux_reloaded", {})):
                    d["aux_reloaded"].update(srv.start())
                    d["aux_reloaded"]["aux"] = load_aux()
            r = chat([{"role": "user", "content": "Reply with the word ok."}], 8)
            AUX["bge"].encode(["after restart"])
            AUX["checker"].score("A.", "A.")
            d["after_restart_chat_s"] = r["total_s"]
            d["recovery"] = "restart with aux resident" if d["aux_resident"].get("ok") else "needed aux unload + reload"
            return d
        run_stage(p("restart"), restart, requires=[p("aux_load")], container=st)

    def teardown():
        return {"server": srv.stop(), "aux": unload_aux()}
    run_stage(p("teardown"), teardown, container=st)
    cfg["memory_overall"] = SAMPLER.close(key + "/overall")
    cfg["server_log_facts"] = srv.facts()
    save_results()


if STATUS.get("helpers") != "ok":
    raise RuntimeError("Cell 7 (helpers) did not finish; fix the error shown there and re-run cells 7 and 8.")
for _i, _u in enumerate(MODE_SPEC["utilizations"]):
    print(f"\n=== {MODE} mode · gpu_memory_utilization = {_u} ===")
    run_config(_u, _i)
print("\nRun finished. Run the next cells for the summary and the download.")
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 9 · Summary for the selected mode (also saved as summary.md)
def _ratio(records, field):
    measured = [r for r in records if field in r]
    return f"{sum(1 for r in measured if r[field])}/{len(measured)}" if measured else "not run"


def summarize():
    summary = {"mode": MODE, "mode_purpose": MODE_SPEC["purpose"], "configs": {}}
    for key, cfg in RESULTS["configs"].items():
        statuses = {s["name"][len(key) + 1:]: s.get("status") for s in cfg["stages"]}
        stage = lambda n: next((s for s in cfg["stages"] if s["name"] == f"{key}/{n}"), {})
        done = [q for q in cfg["questions"] if "pipeline_s" in q]
        warm = [q for q in done if q.get("rep", 1) >= 2] or done
        step = lambda name, field="total_s": stats([q["steps"][name][field] for q in warm if name in q["steps"]])
        fixed = [c for q in warm for c in q.get("check_fixed_claims", []) if c.get("agrees") is not None]
        mem = cfg.get("memory_overall", {})
        facts = cfg.get("server_log_facts", {})
        oom = any(f["kind"] == "oom" and f["stage"].startswith(key + "/") for f in RESULTS["failures"]) or bool(facts.get("oom_in_log"))
        s = {
            "mode": cfg.get("mode", MODE),
            "utilization": cfg["utilization"],
            **evaluate_config(statuses, cfg.get("mode", MODE), CONFIG["repeats"], oom_seen=oom),
            "stage_status": statuses,
            "vllm_ready_s": stage("vllm_start").get("details", {}).get("ready_s"),
            "restart": stage("restart").get("details", {}).get("recovery") if statuses.get("restart") == "ok" else statuses.get("restart", "not run"),
            "cancellation_evidence": cfg.get("cancellation_evidence"),
            "peak_device_used_mib": mem.get("peak_device_used_mib"),
            "device_total_mib": mem.get("device_total_mib"),
            "min_headroom_mib": mem.get("min_headroom_mib"),
            "peak_host_ram_used_mib": mem.get("peak_host_ram_used_mib"),
            "kernel_torch_peak_allocated_mib": mem.get("kernel_torch_peak_allocated_mib"),
            "kv_cache": facts.get("kv_cache_size"),
            "max_concurrency": facts.get("max_concurrency"),
            "attention_backend": facts.get("attention_backend"),
            "questions_measured": len(warm),
            "latency_s": {
                "pipeline_all": stats([q["pipeline_s"] for q in warm]),
                "pipeline_database": stats([q["pipeline_s"] for q in warm if q["route_expected"] == "database"]),
                "pipeline_documents": stats([q["pipeline_s"] for q in warm if q["route_expected"] != "database"]),
                "router": step("router"), "embed_query": step("embed_query"), "sql": step("sql"),
                "report": step("report"), "answer": step("answer"), "check_total": step("check"),
                "report_ttft": step("report", "ttft_s"), "answer_ttft": step("answer", "ttft_s"),
                "checker_per_item": stats([c["latency_s"] for q in warm for c in q.get("check_fixed_claims", []) + q.get("check_answer_sentences", []) if c.get("status") == "ok"]),
            },
            "decode_tok_per_s": stats([q["steps"][n]["decode_tok_per_s"] for q in warm for n in ("report", "answer") if n in q["steps"]]),
            "smoke": {
                "route_match": _ratio(warm, "route_match"),
                "retrieval_hit_at_1": _ratio(warm, "retrieval_hit_at_1"),
                "checker_agrees_with_expected": f"{sum(1 for c in fixed if c['agrees'])}/{len(fixed)}",
                "checker_over_token_limit": sum(q["steps"]["check"]["over_token_limit"] for q in warm if "check" in q["steps"]),
            },
        }
        summary["configs"][key] = s
    summary["incomplete_configs"] = [k for k, s in summary["configs"].items() if s["verdict"] == "incomplete"]
    summary["failed_configs"] = [k for k, s in summary["configs"].items() if s["verdict"] == "failed"]
    summary["candidate"] = choose_candidate(summary["configs"]) if MODE == "target" else None
    summary["failures"] = len(RESULTS["failures"])
    RESULTS["summary"] = summary
    save_results()
    return summary


def _fmt(st):
    return f"{st['median']:.2f} / {st['p90']:.2f} / {st['max']:.2f} (n={st['n']})"


LATENCY_ROWS = [("pipeline_all", "Full pipeline, all measured questions"),
                ("pipeline_database", "Full pipeline, database (router+SQL+report+check)"),
                ("pipeline_documents", "Full pipeline, documents"),
                ("router", "Router"), ("embed_query", "Query embedding (BGE-M3)"), ("sql", "SQL generation"),
                ("report", "Report generation"), ("report_ttft", "Report time to first token"),
                ("answer", "Document answer generation"), ("answer_ttft", "Answer time to first token"),
                ("check_total", "Checker, all items per question"), ("checker_per_item", "Checker, per item")]


def write_markdown(summary):
    env = RESULTS["environment"]
    g = (env.get("gpu") or {}).get("gpus", [{}])[0]
    ve = env.get("vllm_env", {})
    title = "SMALL mode (smoke run, not a target measurement)" if MODE == "small" else "TARGET mode (Qwen3-8B-AWQ benchmark)"
    lines = [f"# EAIOS M1 GPU feasibility — {title}", "", f"Run `{RUN_ID}`. {MODE_SPEC['purpose']}", "",
             f"GPU {g.get('name')} ({g.get('memory_total_mib')} MiB, compute {g.get('compute_cap')}, driver {g.get('driver_version')}, "
             f"CUDA {(env.get('gpu') or {}).get('driver_cuda_version')}) · vLLM {ve.get('vllm')} · torch (vLLM) {ve.get('torch')} · "
             f"torch (kernel) {env.get('kernel_env', {}).get('torch')} · transformers (kernel) {env.get('kernel_env', {}).get('transformers')}", "",
             "Model revisions:", ""]
    lines += [f"- {k}: `{d['repo']}` @ `{d['resolved_sha']}`" + (f" — {d['role']}" if d.get("role") else "")
              for k, d in RESULTS["downloads"].items()]
    lines += ["", f"Workload {RESULTS['workload'].get('version')} sha256 `{RESULTS['workload'].get('sha256')}` "
              "(timing questions accepted by the owner; separate from held-out quality evaluation).", "",
              "The checker is a public zero-shot NLI development baseline, not the fine-tuned checker. Smoke counts are not accuracy results.", ""]
    for key, s in summary["configs"].items():
        verdict = s["verdict"].upper()
        if s["failed_stages"]:
            verdict += " — failed: " + ", ".join(s["failed_stages"])
        if s["oom_seen"]:
            verdict += " — OOM seen"
        if s["incomplete_stages"]:
            verdict += " — not completed: " + ", ".join(f"{i['stage']} ({i['status']})" for i in s["incomplete_stages"])
        lines += [f"## {key} — {verdict}", "", "| Check | Result |", "| --- | --- |"]
        lines += [f"| {name} | {s['stage_status'].get(name, 'not run')} |" for name in s["required_stages"]]
        lines += [f"| vLLM ready (s) | {s['vllm_ready_s']} |"]
        if MODE == "target":
            lines += [f"| Restart recovery | {s['restart']} |"]
            ce = s.get("cancellation_evidence") or {}
            if ce.get("stream"):
                lines += [f"| Cancellation evidence | chunks {ce['stream'].get('chunks')}, client closed unfinished stream "
                          f"{ce['stream'].get('client_closed_early')}, running after {ce.get('requests_running_after')}, "
                          f"follow-up ok {ce.get('follow_up_ok')}; verdict {'ok' if ce['verdict']['ok'] else '; '.join(ce['verdict']['reasons'])} |"]
        lines += [f"| Peak GPU used / total (MiB) | {s['peak_device_used_mib']} / {s['device_total_mib']} |",
                  f"| Minimum headroom (MiB) | {s['min_headroom_mib']} |",
                  f"| Kernel torch peak allocated (MiB) | {s['kernel_torch_peak_allocated_mib']} |",
                  f"| Peak host RAM used (MiB) | {s['peak_host_ram_used_mib']} |",
                  f"| KV cache | {s['kv_cache']} |", f"| Attention backend | {s['attention_backend']} |", ""]
        rows = [(label, s["latency_s"][k]) for k, label in LATENCY_ROWS if s["latency_s"].get(k)]
        if s["decode_tok_per_s"]:
            rows.append(("Decode tokens/s", s["decode_tok_per_s"]))
        if rows:
            which = "warm passes" if MODE == "target" else "single measured question"
            lines += [f"| Latency, {which} (s) | median / p90 / max |", "| --- | --- |"]
            lines += [f"| {label} | {_fmt(st)} |" for label, st in rows]
            lines += [""]
        sm = s["smoke"]
        lines += [f"Smoke: routes {sm['route_match']}, retrieval hit@1 {sm['retrieval_hit_at_1']}, checker agrees with expected "
                  f"labels {sm['checker_agrees_with_expected']}, checker inputs over 512 tokens {sm['checker_over_token_limit']}.", ""]
    lines += ["## Failures", ""]
    lines += [f"- `{f['stage']}` ({f['kind']}): {f['message'][:400]}" for f in RESULTS["failures"]] or ["None recorded."]
    if MODE == "target":
        lines += ["", "Incomplete configurations (never recommended): " + (", ".join(summary["incomplete_configs"]) or "none"),
                  "", f"Candidate configuration (passed every required stage including restart, most headroom): {summary['candidate']}",
                  "", "Specification planning estimate for comparison: 25–75 s per warm answer with vLLM (section 6)."]
    else:
        lines += ["", "Small mode makes no configuration recommendation. Run MODE = \"target\" for the Qwen3-8B-AWQ measurements."]
    (OUT / "summary.md").write_text("\n".join(lines))
    save_results()
    return "\n".join(lines)


_summary = summarize()
try:
    from IPython.display import Markdown, display
    display(Markdown(write_markdown(_summary)))
except Exception:
    print(write_markdown(_summary))
''')

# ---------------------------------------------------------------------------
code(r'''
# @title 10 · Download the results (zip of results.json, summary.md, workload and logs) — safe to run any time
RESULTS["finished_at"] = utcnow()
save_results()
_zip = shutil.make_archive(str(BASE / f"eaios-m1-gpu-{MODE}-{RUN_ID}"), "zip", root_dir=OUT.parent, base_dir=OUT.name)
print("Results zip:", _zip)
if DRIVE_DIR is not None:
    shutil.copy(_zip, DRIVE_DIR.parent)
    print("Also copied to Drive:", DRIVE_DIR.parent / pathlib.Path(_zip).name)
try:
    from google.colab import files
    files.download(_zip)
except Exception as exc:
    print("Automatic download unavailable here; use the Files panel on the left.", exc)
''')


BENCH_CORE = HERE / "bench_core.py"
MARKER = "# <<BENCH_CORE>>"


def render() -> str:
    core = BENCH_CORE.read_text().rstrip("\n")
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"gpuType": "T4", "provenance": [], "name": "m1_gpu_feasibility.ipynb"},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "cells": [],
    }
    for i, (kind, src) in enumerate(CELLS):
        if MARKER in src:
            src = src.replace(MARKER, core)
        cell = {"cell_type": kind, "id": f"cell-{i:02d}", "metadata": {}, "source": src.splitlines(keepends=True)}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        nb["cells"].append(cell)
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


def build() -> None:
    TARGET.write_text(render())
    print(f"wrote {TARGET.relative_to(HERE.parent.parent)} ({len(CELLS)} cells)")


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        if TARGET.read_text() != render():
            sys.exit("m1_gpu_feasibility.ipynb is stale: run python ml/gpu-feasibility/build_notebook.py")
        print("notebook is up to date")
    else:
        build()
