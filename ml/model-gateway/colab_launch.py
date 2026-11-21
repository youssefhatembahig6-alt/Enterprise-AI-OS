"""Start the measured M2 model stack inside a Colab T4 kernel.

Run in a Colab cell after installing the pinned vLLM environment exactly as the M1
notebook does (ml/gpu-feasibility/vllm-env-pins.txt in /content/vllm-env):

    import os, secrets
    os.environ["GATEWAY_TOKEN"] = secrets.token_urlsafe(32)   # copy into the backend .env
    %run /content/Enterprise-AI-OS/ml/model-gateway/colab_launch.py

It downloads the three models at their pinned revisions, starts vLLM on 127.0.0.1 with
the measured flags (gpu_memory_utilization 0.75, max_model_len 4096), loads BGE-M3
and the DeBERTa development checker in this kernel, and serves the gateway on
127.0.0.1:8100. Exposing port 8100 through a tunnel is a separate, explicit step
(README.md): check that it is permitted under your Colab usage terms first.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gateway  # noqa: E402

MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/content/models"))
VLLM_ENV = Path(os.environ.get("VLLM_ENV", "/content/vllm-env"))
VLLM_PORT = int(os.environ.get("VLLM_PORT", "8000"))
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8100"))


def download(key: str) -> str:
    from huggingface_hub import snapshot_download

    spec = gateway.PINNED[key]
    target = MODELS_DIR / key
    path = snapshot_download(spec["model"], revision=spec["revision"], local_dir=str(target))
    print(f"{key}: {spec['model']}@{spec['revision']} -> {path}")
    return path


def wait_ready(url: str, api_key: str, timeout: float = 1500.0) -> float:
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    while time.monotonic() - started < timeout:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local
                if response.status == 200:
                    return time.monotonic() - started
        except OSError:
            time.sleep(3)
    raise RuntimeError("vLLM did not become ready")


def main() -> None:
    token = os.environ.get("GATEWAY_TOKEN")
    if not token or len(token) < 32:
        raise SystemExit("set GATEWAY_TOKEN (at least 32 characters) before launching")
    chat_path, embed_path, checker_path = download("chat"), download("embed"), download("checker")

    vllm_key = secrets.token_urlsafe(24)
    command = gateway.vllm_command(chat_path, VLLM_PORT, vllm_key, str(VLLM_ENV / "bin" / "vllm"))
    env = dict(os.environ, HF_HUB_OFFLINE="1", VLLM_NO_USAGE_STATS="1", DO_NOT_TRACK="1",
               PATH=f"{VLLM_ENV / 'bin'}:{os.environ.get('PATH', '')}")
    env.pop("VIRTUAL_ENV", None)
    log = open("/content/vllm-m2.log", "w")  # noqa: SIM115 - lives as long as the server
    subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
    ready = wait_ready(f"http://127.0.0.1:{VLLM_PORT}/v1/models", vllm_key)
    print(f"vLLM ready in {ready:.1f}s with {' '.join(command[3:]).replace(vllm_key, '***')}")

    os.environ.update(
        VLLM_URL=f"http://127.0.0.1:{VLLM_PORT}", VLLM_API_KEY=vllm_key,
        EMBED_PATH=embed_path, CHECKER_PATH=checker_path,
    )
    import uvicorn

    app = gateway.colab_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=GATEWAY_PORT, log_level="info"))
    threading.Thread(target=server.run, daemon=True).start()
    print(f"gateway listening on 127.0.0.1:{GATEWAY_PORT} (bearer GATEWAY_TOKEN required)")


if __name__ == "__main__":
    main()
