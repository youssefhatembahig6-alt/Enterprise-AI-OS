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
from typing import Any, ClassVar

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

    def encode_with_sparse(self, texts: list[str]) -> list[tuple[list[float], dict[int, float]]]:
        return [([0.5] * 1024, {7: 0.25, len(t): 1.5}) for t in texts]


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


def test_hybrid_embeddings_round_trip(gateway_client: Any) -> None:
    _, client = gateway_client
    vectors = backend_client(client).embed_text(["abc", "hello"])
    assert len(vectors) == 2 and len(vectors[0].dense) == 1024
    assert vectors[0].sparse == {7: 0.25, 3: 1.5}
    assert vectors[1].sparse == {7: 0.25, 5: 1.5}


class StubParser:
    info: ClassVar[dict[str, str]] = {"model": "docling", "version": "stub"}

    def __init__(self) -> None:
        self.seen: list[tuple[str, bytes]] = []

    def parse(self, filename: str, data: bytes) -> dict[str, Any]:
        self.seen.append((filename, data))
        return {
            "parser": "docling-stub",
            "elements": [
                {"type": "heading", "text": "Results", "page": 1, "heading_path": ["Results"]},
                {
                    "type": "table",
                    "text": "Region | Net\nEMEA | 1.0",
                    "page": 1,
                    "heading_path": ["Results"],
                    "header_end": 12,
                },
            ],
            "notes": {"docling_version": "stub"},
        }


def test_docling_parse_route_feeds_the_backend_parser() -> None:
    from app.documents.parsers import parse

    gateway = load_gateway()
    stub = StubParser()
    app = gateway.create_app(
        gateway.GatewayConfig(token=TOKEN), embedder=None, checker=None, parser=stub
    )
    pdf = b"%PDF-1.4\n" + b"x" * (400 * 1024)  # larger than the 256 KB JSON route limit
    with TestClient(app, base_url=ROOT) as client:
        model = backend_client(client)
        assert model.served_models().raw["models"]["parser"]["model"] == "docling"
        doc = parse("report.pdf", pdf, docling=model.parse_document)
        assert client.post("/parse", json={}).status_code == 401
    assert stub.seen == [("report.pdf", pdf)]
    assert doc.parser == "docling-stub"
    assert doc.text == "Results\n\nRegion | Net\nEMEA | 1.0"
    table = doc.elements[1]
    assert doc.text[table.start : table.end].startswith("Region | Net")
    assert table.element.header_end == 12


def test_gateway_without_docling_triggers_the_documented_fallback() -> None:
    from app.documents.parsers import DoclingUnavailable

    gateway = load_gateway()
    app = gateway.create_app(gateway.GatewayConfig(token=TOKEN), embedder=None, checker=None)
    with TestClient(app, base_url=ROOT) as client:
        response = client.post(
            "/parse",
            json={"filename": "a.pdf", "content_base64": "JVBERg=="},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "parser_not_loaded"
        with pytest.raises(Exception) as caught:
            backend_client(client).parse_document("a.pdf", b"%PDF-1.4")
    assert not isinstance(caught.value, DoclingUnavailable)  # ingestion maps it


def test_docling_subprocess_runs_the_worker_in_its_own_interpreter(tmp_path: Path) -> None:
    gateway = load_gateway()
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, sys\n"
        "data = open(sys.argv[1], 'rb').read()\n"
        "json.dump({'parser': 'docling-x', 'elements': [], 'notes': {'size': len(data)}},"
        " sys.stdout)\n"
    )
    runner = gateway.DoclingSubprocess.__new__(gateway.DoclingSubprocess)
    runner._python, runner._timeout, runner._worker = sys.executable, 30.0, str(worker)
    assert runner.parse("x.pdf", b"12345")["notes"] == {"size": 5}
    worker.write_text("import sys\nsys.exit('boom')\n")
    with pytest.raises(gateway.ParseError, match="boom"):
        runner.parse("x.pdf", b"1")
