"""EAIOS model gateway for the Colab GPU runtime (M2).

One root URL, four routes (docs/contracts/model-gateway.md):

    GET  /health                 served models, pinned revisions and engine settings
    POST /v1/chat/completions    streamed proxy to the local vLLM (Qwen3-8B-AWQ)
    POST /v1/embeddings          BGE-M3 dense query embeddings (not used by M2 reports)
    POST /check                  NLI claim checker (public DeBERTa baseline, development only)

Every route except /health requires ``Authorization: Bearer $GATEWAY_TOKEN``. The
gateway is an inference service, not an authority: it receives only the text the
backend chose to send, holds no database credentials and cannot reach the backend's
services. vLLM listens on 127.0.0.1 with its own key; only this gateway is exposed.

The model objects are injected, so the HTTP contract is tested on CPU with stubs
(backend/tests/unit/test_model_gateway_contract.py) and the same file runs in Colab
with the real models (see README.md).
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Measured M1 target configuration (ml/gpu-feasibility/reports/2026-11-20-target-t4.md).
PINNED = {
    "chat": {"model": "Qwen/Qwen3-8B-AWQ", "revision": "4da05a8edb55c6046cce958586c33b61da07bb79"},
    "embed": {"model": "BAAI/bge-m3", "revision": "5617a9f61b028005a4858fdac845db406aefb181"},
    "checker": {
        "model": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
        "revision": "6f5cf0a2b59cabb106aca4c287eed12e357e90eb",
        "status": "development_baseline_not_final_trained_checker",
    },
}
VLLM_ARGS = {
    "quantization": "awq",
    "dtype": "half",
    "max_model_len": 4096,
    "max_num_seqs": 8,
    "gpu_memory_utilization": 0.75,
    "seed": 0,
}
CHECKER_MAX_TOKENS = 512
MAX_BODY_BYTES = 256 * 1024
MAX_CHECK_PAIRS = 16
MAX_EMBED_INPUTS = 64


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class Checker(Protocol):
    def score(self, premise: str, hypothesis: str) -> dict[str, Any]: ...


@dataclass
class GatewayConfig:
    token: str
    vllm_url: str = "http://127.0.0.1:8000"
    vllm_api_key: str = ""
    vllm_version: str = "0.11.0"
    max_concurrent_chats: int = 4
    served: dict[str, dict[str, str]] = field(default_factory=lambda: PINNED)
    engine: dict[str, Any] = field(default_factory=lambda: dict(VLLM_ARGS))


def vllm_command(model_path: str, port: int, api_key: str, vllm_cli: str = "vllm") -> list[str]:
    """The exact vLLM command for the measured configuration."""

    return [
        vllm_cli, "serve", model_path,
        "--served-model-name", PINNED["chat"]["model"],
        "--host", "127.0.0.1", "--port", str(port),
        "--quantization", VLLM_ARGS["quantization"], "--dtype", VLLM_ARGS["dtype"],
        "--max-model-len", str(VLLM_ARGS["max_model_len"]),
        "--max-num-seqs", str(VLLM_ARGS["max_num_seqs"]),
        "--gpu-memory-utilization", str(VLLM_ARGS["gpu_memory_utilization"]),
        "--seed", str(VLLM_ARGS["seed"]),
        "--api-key", api_key,
    ]  # fmt: skip


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code}})


def create_app(
    config: GatewayConfig,
    *,
    embedder: Embedder | None,
    checker: Checker | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    chat_slots = asyncio.Semaphore(config.max_concurrent_chats)
    upstream = httpx.AsyncClient(
        base_url=config.vllm_url,
        headers={"Authorization": f"Bearer {config.vllm_api_key}"} if config.vllm_api_key else {},
        timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
        transport=transport,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await upstream.aclose()

    app = FastAPI(
        title="EAIOS model gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.encode(), config.token.encode()
        )

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[..., Any]) -> Any:
        if request.url.path != "/health" and not authorized(request):
            return _error(401, "unauthorized")
        length = request.headers.get("content-length")
        if length is not None and int(length) > MAX_BODY_BYTES:
            return _error(413, "body_too_large")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        models = {k: dict(v) for k, v in config.served.items()}
        if embedder is None:
            models.pop("embed", None)
        if checker is None:
            models.pop("checker", None)
        return {
            "status": "ok",
            "models": models,
            "engine": {**config.engine, "vllm_version": config.vllm_version},
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        body = await request.json()
        if body.get("model") != config.served["chat"]["model"] or not body.get("stream"):
            return _error(400, "unsupported_request")
        if chat_slots.locked():
            return _error(429, "busy")

        async def relay() -> AsyncIterator[bytes]:
            async with chat_slots:
                # Closing this generator (client disconnect or cancellation) closes the
                # upstream stream, which makes vLLM abort the request.
                async with upstream.stream("POST", "/v1/chat/completions", json=body) as response:
                    if response.status_code != 200:
                        yield b'data: {"error": {"code": "engine_error"}}\n\n'
                        return
                    async for chunk in response.aiter_bytes():
                        yield chunk

        return StreamingResponse(relay(), media_type="text/event-stream")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Any:
        if embedder is None:
            return _error(503, "embedder_not_loaded")
        body = await request.json()
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list) or not inputs or len(inputs) > MAX_EMBED_INPUTS:
            return _error(400, "invalid_input")
        vectors = await asyncio.to_thread(embedder.encode, [str(i) for i in inputs])
        return {
            "object": "list",
            "model": config.served["embed"]["model"],
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
        }

    @app.post("/check")
    async def check(request: Request) -> Any:
        if checker is None:
            return _error(503, "checker_not_loaded")
        body = await request.json()
        pairs = body.get("pairs")
        if not isinstance(pairs, list) or not pairs or len(pairs) > MAX_CHECK_PAIRS:
            return _error(400, "invalid_pairs")
        results = []
        for pair in pairs:
            premise, hypothesis = str(pair.get("premise", "")), str(pair.get("hypothesis", ""))
            scored = await asyncio.to_thread(checker.score, premise, hypothesis)
            if scored.get("status") != "ok":
                # Never truncate silently: an over-long pair is an error, not a score.
                return _error(422, str(scored.get("status", "check_failed")))
            probs = scored["probs"]
            results.append(
                {
                    "entailment": probs.get("entailment", 0.0),
                    "neutral": probs.get("neutral", 0.0),
                    "contradiction": probs.get("contradiction", 0.0),
                    "tokens": scored.get("tokens"),
                }
            )
        checker_info = config.served["checker"]
        return {"results": results, "model": checker_info["model"],
                "revision": checker_info["revision"], "status": checker_info.get("status")}

    return app


# --- real models (Colab GPU only) -------------------------------------------------------


class BgeM3Dense:
    """BGE-M3 dense CLS embedding, normalized (as in the M1 benchmark)."""

    def __init__(self, path: str) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModel.from_pretrained(path, torch_dtype=torch.float16).to("cuda").eval()

    def encode(self, texts: list[str]) -> list[list[float]]:
        torch = self.torch
        with torch.inference_mode():
            batch = self.tok(texts, padding=True, truncation=False, return_tensors="pt")
            if int(batch["attention_mask"].sum(1).max()) > 8192:
                raise ValueError("input exceeds 8192 tokens; refusing to truncate")
            hidden = self.model(**{k: v.to("cuda") for k, v in batch.items()}).last_hidden_state
            dense = torch.nn.functional.normalize(hidden[:, 0].float(), dim=-1)
        return dense.cpu().tolist()


class NliChecker:
    """DeBERTa NLI baseline; refuses inputs over the checker's token limit."""

    def __init__(self, path: str) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = (
            AutoModelForSequenceClassification.from_pretrained(path, torch_dtype=torch.float16)
            .to("cuda")
            .eval()
        )
        self.labels = {int(k): v.lower() for k, v in self.model.config.id2label.items()}

    def score(self, premise: str, hypothesis: str) -> dict[str, Any]:
        torch = self.torch
        enc = self.tok(premise, hypothesis, truncation=False, return_tensors="pt")
        tokens = int(enc["input_ids"].shape[1])
        if tokens > CHECKER_MAX_TOKENS:
            return {"status": "over_token_limit", "tokens": tokens}
        with torch.inference_mode():
            logits = self.model(**{k: v.to("cuda") for k, v in enc.items()}).logits.float()
            probs = torch.softmax(logits, -1)[0].cpu().tolist()
        if any(p != p for p in probs):
            return {"status": "nan_output", "tokens": tokens}
        named = {self.labels[i]: round(p, 6) for i, p in enumerate(probs)}
        normalized = {
            "entailment": next((v for k, v in named.items() if k.startswith("entail")), 0.0),
            "neutral": next((v for k, v in named.items() if k.startswith("neutral")), 0.0),
            "contradiction": next((v for k, v in named.items() if k.startswith("contra")), 0.0),
        }
        return {"status": "ok", "tokens": tokens, "probs": normalized}


def colab_app() -> FastAPI:
    """Build the gateway from environment variables inside the Colab kernel."""

    config = GatewayConfig(
        token=os.environ["GATEWAY_TOKEN"],
        vllm_url=os.environ.get("VLLM_URL", "http://127.0.0.1:8000"),
        vllm_api_key=os.environ["VLLM_API_KEY"],
        vllm_version=os.environ.get("VLLM_VERSION", "0.11.0"),
    )
    return create_app(
        config,
        embedder=BgeM3Dense(os.environ["EMBED_PATH"]) if os.environ.get("EMBED_PATH") else None,
        checker=NliChecker(os.environ["CHECKER_PATH"]),
    )
