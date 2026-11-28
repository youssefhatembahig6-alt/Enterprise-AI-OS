"""Model status for signed-in users (M8): which inference the server is using and, in
gateway mode, whether the gateway is serving the pinned chat model and revision and
accepts the server's token.

``/ready`` deliberately never claims model capability. This endpoint runs the same check
every real run makes (``ModelClient.served_models``) and then the gateway's authenticated
``/ready``, which asks the chat server itself (vLLM's /health and /v1/models), so
``verified`` means the gateway accepted the server's bearer token AND the actual Qwen
chat server is up serving the pinned model and revision (``/health`` alone is public). The
web UI shows the served model or a typed failure. It returns model names, revisions and
a failure class only; never the gateway URL or token.
"""

from __future__ import annotations

import threading
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.api.deps import CurrentContext
from app.config import ModelMode
from app.reporting.model_client import FAKE_LABEL, ModelClient, ModelError

router = APIRouter(prefix="/api/v1", tags=["model"])
CACHE_SECONDS = 15.0


class ServedModel(BaseModel):
    model: str | None = None
    revision: str | None = None


class ModelStatus(BaseModel):
    mode: Literal["fake", "gateway"]
    verified: bool
    authenticated: bool = False  # token accepted and the chat server ready (gateway mode)
    chat: ServedModel | None = None
    embed: ServedModel | None = None
    checker: ServedModel | None = None
    engine: dict[str, Any] | None = None
    error: str | None = None  # model failure class, e.g. "unavailable", "revision_mismatch"
    label: str | None = None
    checked_at: float


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: ModelStatus | None = None


def _served(models: dict[str, Any], key: str) -> ServedModel | None:
    entry = models.get(key)
    if not isinstance(entry, dict):
        return None
    return ServedModel(model=entry.get("model"), revision=entry.get("revision"))


def check_status(model: ModelClient) -> ModelStatus:
    now = time.time()
    if model.mode is ModelMode.FAKE:
        return ModelStatus(mode="fake", verified=False, label=FAKE_LABEL, checked_at=now)
    try:
        raw = model.served_models().summary()
        model.ready()  # authenticated: the gateway accepted the token AND vLLM serves Qwen
    except ModelError as exc:
        return ModelStatus(mode="gateway", verified=False, error=exc.code, checked_at=now)
    models_raw, engine_raw = raw.get("models"), raw.get("engine")
    models: dict[str, Any] = models_raw if isinstance(models_raw, dict) else {}
    engine: dict[str, Any] = engine_raw if isinstance(engine_raw, dict) else {}
    keep = ("quantization", "dtype", "max_model_len", "gpu_memory_utilization", "vllm_version")
    return ModelStatus(
        mode="gateway",
        verified=True,
        authenticated=True,
        chat=_served(models, "chat"),
        embed=_served(models, "embed"),
        checker=_served(models, "checker"),
        engine={k: engine[k] for k in keep if k in engine},
        checked_at=now,
    )


@router.get("/model/status", response_model=ModelStatus)
def model_status(
    request: Request,
    _ctx: CurrentContext,
    refresh: Annotated[bool, Query(description="Skip the 15 s cache (explicit recheck).")] = False,
) -> ModelStatus:
    """The inference mode and, for the real gateway, the verified served chat model."""

    cache: _Cache = request.app.state.model_status_cache
    with cache.lock:
        fresh = cache.value is not None and time.time() - cache.value.checked_at < CACHE_SECONDS
        if fresh and not refresh:
            return cache.value  # type: ignore[return-value]
    status = check_status(request.app.state.model)
    with cache.lock:
        cache.value = status
    return status


def new_cache() -> _Cache:
    return _Cache()
