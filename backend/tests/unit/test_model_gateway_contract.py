"""Backend client against the real Colab gateway app, with stub models (CPU only).

Proves the HTTP contract end to end: one root URL, explicit routes, service auth,
streamed chat relay, checker and embedder payloads, health revisions, and refusal of
over-long checker inputs instead of truncation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.reporting.model_client import GatewayAuthError, GatewayModelClient, GatewayProtocolError

GATEWAY_FILE = Path(__file__).resolve().parents[3] / "ml" / "model-gateway" / "gateway.py"
pytestmark = pytest.mark.skipif(not GATEWAY_FILE.exists(), reason="ml/ not in this build")
ROOT = "https://colab-gateway.example.test"
TOKEN = "gateway-token-for-tests-0000"


def load_gateway() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eaios_model_gateway", GATEWAY_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


class StubEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0] for t in texts]


class StubChecker:
    def score(self, premise: str, hypothesis: str) -> dict[str, Any]:
        if len(hypothesis) > 300:
            return {"status": "over_token_limit", "tokens": 700}
        entail = 0.95 if hypothesis in premise else 0.1
        return {
            "status": "ok",
            "tokens": 40,
            "probs": {"entailment": entail, "neutral": 0.0, "contradiction": 1 - entail},
        }


def vllm_stub(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer vllm-local-key"
    body = json.loads(request.content)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    chunks = [
        {"choices": [{"delta": {"content": '{"sql": '}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": '"SELECT 1"}'}, "finish_reason": "stop"}]},
    ]
    payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200, content=payload.encode(), headers={"content-type": "text/event-stream"}
    )


@pytest.fixture
def gateway_client() -> Any:
    gateway = load_gateway()
    app = gateway.create_app(
        gateway.GatewayConfig(token=TOKEN, vllm_api_key="vllm-local-key"),
        embedder=StubEmbedder(),
        checker=StubChecker(),
        transport=httpx.MockTransport(vllm_stub),
    )
    with TestClient(app, base_url=ROOT) as client:
        yield gateway, client


def backend_client(test_client: TestClient, token: str = TOKEN) -> GatewayModelClient:
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://x@localhost/none",
        REDIS_URL="redis://localhost:6379/0",
        QDRANT_URL="http://localhost:6333",
        S3_ENDPOINT_URL="http://localhost:8333",
        JWT_SECRET="unit-test-secret-unit-test-secret-0000",
        LLM_BASE_URL=ROOT,
        MODEL_GATEWAY_TOKEN=token,
    )
    return GatewayModelClient(settings, transport=test_client._transport)


def test_backend_and_gateway_agree_on_every_route(gateway_client: Any) -> None:
    gateway, client = gateway_client
    model = backend_client(client)
    served = model.served_models().raw
    assert served["models"]["chat"] == gateway.PINNED["chat"]
    assert served["engine"]["gpu_memory_utilization"] == 0.75
    assert served["engine"]["max_model_len"] == 4096
    chat = model.chat([{"role": "user", "content": "q"}], purpose="sql", max_tokens=20)
    assert chat.text == '{"sql": "SELECT 1"}' and chat.finish_reason == "stop"
    scores = model.check([("Net sales were USD 1.00.", "Net sales were USD 1.00.")])
    assert scores[0].entailment == 0.95
    assert model.embed(["abc"]) == [[3.0, 0.0]]


def test_gateway_requires_the_service_token(gateway_client: Any) -> None:
    _, client = gateway_client
    with pytest.raises(GatewayAuthError):
        backend_client(client, token="wrong-token-wrong-token-00").check([("a", "b")])
    assert client.get("/health").status_code == 200  # health is public, no content
    assert client.post("/check", json={"pairs": []}).status_code == 401


def test_over_long_checker_input_is_refused_not_truncated(gateway_client: Any) -> None:
    _, client = gateway_client
    with pytest.raises(GatewayProtocolError):
        backend_client(client).check([("premise", "x" * 400)])


def test_vllm_command_uses_the_measured_configuration() -> None:
    gateway = load_gateway()
    command = gateway.vllm_command("/models/qwen", 8000, "key")
    joined = " ".join(command)
    for flag in (
        "--gpu-memory-utilization 0.75",
        "--max-model-len 4096",
        "--max-num-seqs 8",
        "--quantization awq",
        "--dtype half",
        "--host 127.0.0.1",
    ):
        assert flag in joined
