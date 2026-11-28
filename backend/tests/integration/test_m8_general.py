"""M8 General chat mode and model status, on real PostgreSQL through the real API.

The model is the real ``GatewayModelClient`` (gateway mode) talking to a scripted gateway
over ``httpx.MockTransport``: the backend code paths are the ones a Colab gateway
exercises (served-model check, streamed chat, typed failures); only the GPU is absent.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.reports import choose_kind
from app.general.workflow import SYSTEM_PROMPT
from app.main import create_app
from app.reporting.model_client import GatewayModelClient
from tests.integration.conftest import (
    DbHarness,
    disposable_database,
    document_settings,
    login,
    tenant_packs,
)
from tests.integration.document_support import auth
from tests.integration.report_support import ask, replay

ROOT = "https://gpu-gateway.example.test"
TOKEN = "service-token-for-m8-tests-only"
CHAT = "Qwen/Qwen3-8B-AWQ"
REVISION = "4da05a8edb55c6046cce958586c33b61da07bb79"
FINANCE = "niletech/finance-reader"
SALES = "niletech/sales-reader"
REPLY = "Rivers carry sediment downstream.\n\nThat is why deltas grow over time."


def health(chat_revision: str = REVISION) -> dict[str, Any]:
    return {
        "models": {
            "chat": {"model": CHAT, "revision": chat_revision},
            "embed": {
                "model": "BAAI/bge-m3",
                "revision": "5617a9f61b028005a4858fdac845db406aefb181",
            },
            "checker": {
                "model": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
                "revision": "6f5cf0a2b59cabb106aca4c287eed12e357e90eb",
            },
        },
        "engine": {"gpu_memory_utilization": 0.75, "max_model_len": 4096, "quantization": "awq"},
    }


def sse(text: str) -> bytes:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


@dataclass
class Gateway:
    """Scripted gateway: health payload/status and chat reply; records chat requests."""

    health_status: int = 200
    health_body: dict[str, Any] = field(default_factory=health)
    reply: str = REPLY
    reject_token: bool = False
    qwen_up: bool = True
    chats: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if request.url.path.endswith("/health"):
            return httpx.Response(self.health_status, json=self.health_body)
        if request.url.path.endswith("/ready"):
            if self.reject_token:
                return httpx.Response(401, json={"error": {"code": "unauthorized"}})
            if not self.qwen_up:  # gateway process up, chat server (vLLM) down
                return httpx.Response(503, json={"error": {"code": "chat_server_unreachable"}})
            chat = self.health_body["models"]["chat"]
            return httpx.Response(200, json={"status": "ready", "chat": {**chat, "ready": True}})
        if request.url.path.endswith("/embeddings"):
            if self.reject_token:
                return httpx.Response(401, json={"error": "unauthorized"})
            body = json.loads(request.content)
            return httpx.Response(
                200, json={"data": [{"embedding": [0.0] * 1024} for _ in body["input"]]}
            )
        if request.url.path.endswith("/chat/completions"):
            with self.lock:
                self.chats.append(json.loads(request.content))
            return httpx.Response(200, content=sse(self.reply))
        return httpx.Response(404)

    def reset(self) -> None:
        self.health_status, self.health_body, self.reply = 200, health(), REPLY
        self.reject_token = False
        self.qwen_up = True
        self.chats.clear()


@dataclass
class M8:
    client: TestClient
    db: DbHarness
    gateway: Gateway

    def token(self, ref: str) -> str:
        return login(self.client, self.db, ref)

    def status(self, ref: str = FINANCE) -> dict[str, Any]:
        self.client.app.state.model_status_cache.value = None  # type: ignore[attr-defined]
        r = self.client.get("/api/v1/model/status", headers=auth(self.token(ref)))
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body


@pytest.fixture(scope="module")
def m8(
    stub_http_url: str, qdrant_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[M8]:
    tmp: Path = tmp_path_factory.mktemp("m8")
    gateway = Gateway()
    with disposable_database(seed=True) as db:
        settings = document_settings(
            db,
            stub_http_url,
            qdrant_url,
            str(tmp / "objects"),
            "m8" + uuid.uuid4().hex[:8],
            MODEL_MODE="gateway",
            LLM_BASE_URL=ROOT,
            MODEL_GATEWAY_TOKEN=TOKEN,
        )
        model = GatewayModelClient(settings, transport=httpx.MockTransport(gateway))
        with TestClient(create_app(settings, model=model)) as client:
            yield M8(client, db, gateway)


@pytest.fixture(autouse=True)
def _fresh_gateway(m8: M8) -> None:
    m8.gateway.reset()


def general(m8: M8, ref: str, question: str) -> Any:
    return ask(m8.client, m8.token(ref), question, mode="general")


def test_general_reply_is_the_real_gateway_model_alone(m8: M8) -> None:
    question = "Explain in two sentences why river deltas grow."
    stream = general(m8, FINANCE, question)
    assert stream.status_code == 200
    assert stream.done["outcome"] == "published", stream.raw[-600:]
    verification = stream.done["verification"]
    assert verification["label"] == "general_model_reply_unverified"
    assert verification["company_data"] == "not_used"
    assert verification["checked_against_sources"] is False
    assert verification["inference_mode"] == "gateway"
    assert verification["model"] == {"name": CHAT, "revision": REVISION}
    # Every labelled event is real gateway inference; no fake label anywhere.
    for frame in stream.frames:
        if "inference" in frame.data:
            assert frame.data["inference"] == "gateway", frame
    assert "FAKE" not in stream.raw
    assert [f.data["text"] for f in stream.of("token")] == REPLY.split("\n\n")
    assert all(f.data["citations"] == [] for f in stream.of("token"))
    assert stream.of("citation") == []
    # The model received only the general system prompt and the question: no passages,
    # rows, tools or earlier turns.
    (request,) = m8.gateway.chats
    assert request["model"] == CHAT
    assert request["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    assert "tools" not in request
    steps = [f.data["step"] for f in stream.of("status")]
    assert "retrieval" not in steps and "sql_generation" not in steps
    assert "general_answer" in steps


def test_auto_never_routes_to_general() -> None:
    for question in ("Write a haiku about rivers", "What is a balance sheet?", "hello"):
        assert choose_kind(question, "auto") != "general"
    assert choose_kind("anything", "general") == "general"


def test_revision_mismatch_fails_visibly_before_any_chat_call(m8: M8) -> None:
    m8.gateway.health_body = health(chat_revision="0" * 40)
    stream = general(m8, FINANCE, "What is photosynthesis?")
    assert stream.done["outcome"] == "failed"
    (error,) = [f.data for f in stream.of("error")]
    assert error["code"] == "model_unavailable"
    assert error["detail"] == "revision_mismatch"
    assert m8.gateway.chats == []
    status = m8.status()
    assert status["mode"] == "gateway"
    assert status["verified"] is False
    assert status["error"] == "revision_mismatch"


def test_unreachable_gateway_fails_with_its_class(m8: M8) -> None:
    m8.gateway.health_status = 503
    stream = general(m8, FINANCE, "What is photosynthesis?")
    (error,) = [f.data for f in stream.of("error")]
    assert (error["code"], error["detail"]) == ("model_unavailable", "unavailable")
    assert m8.status()["error"] == "unavailable"


def test_documents_check_the_served_model_too(m8: M8) -> None:
    m8.gateway.health_body = health(chat_revision="1" * 40)
    stream = ask(
        m8.client, m8.token(FINANCE), "How many leave days do staff get?", mode="documents"
    )
    assert stream.done["outcome"] == "failed"
    (error,) = [f.data for f in stream.of("error")]
    assert error["detail"] == "revision_mismatch"


def test_model_status_reports_the_verified_served_model(m8: M8) -> None:
    status = m8.status()
    assert status["verified"] is True
    assert status["authenticated"] is True
    assert status["chat"] == {"model": CHAT, "revision": REVISION}
    assert status["engine"]["max_model_len"] == 4096
    assert status["error"] is None
    body = json.dumps(status)
    assert ROOT not in body and TOKEN not in body
    assert m8.client.get("/api/v1/model/status").status_code == 401


def test_general_pack_is_backend_enforced(m8: M8) -> None:
    packs = ["documents", "sales_reports", "legal", "code", "actions"]
    with tenant_packs(m8.db, "niletech", packs):
        r = m8.client.post(
            "/api/v1/chat",
            json={"question": "hello", "mode": "general"},
            headers=auth(m8.token(FINANCE)),
        )
    assert r.status_code == 403, r.text
    assert m8.gateway.chats == []


def test_general_reply_is_owner_only_and_never_an_email_source(m8: M8) -> None:
    stream = general(m8, FINANCE, "Give one tip for writing clear emails.")
    assert stream.done["outcome"] == "published" and stream.run_id
    assert replay(m8.client, m8.token(SALES), stream.run_id).status_code == 404
    owner = replay(m8.client, m8.token(FINANCE), stream.run_id)
    assert [f.data["text"] for f in owner.of("token")] == REPLY.split("\n\n")
    to = str(m8.db.foundation.tenants["niletech"].users["finance-reader"].id)
    r = m8.client.post(
        "/api/v1/actions/email-drafts",
        json={"source_run_id": stream.run_id, "recipient_user_id": to},
        headers=auth(m8.token(FINANCE)),
    )
    assert r.status_code in (403, 404, 422), r.text
    assert r.status_code != 201


def test_status_requires_authenticated_gateway_access_and_refresh_skips_cache(m8: M8) -> None:
    token = m8.token(FINANCE)
    m8.client.app.state.model_status_cache.value = None  # type: ignore[attr-defined]
    ok = m8.client.get("/api/v1/model/status", headers=auth(token)).json()
    assert (ok["verified"], ok["authenticated"]) == (True, True)
    # /health still reports the pinned model, but the gateway now rejects the token.
    m8.gateway.reject_token = True
    cached = m8.client.get("/api/v1/model/status", headers=auth(token)).json()
    assert cached["verified"] is True  # within the 15 s cache
    fresh = m8.client.get("/api/v1/model/status?refresh=true", headers=auth(token)).json()
    assert (fresh["verified"], fresh["authenticated"], fresh["error"]) == (
        False,
        False,
        "auth_failed",
    )


def test_connected_needs_the_qwen_chat_server_not_just_the_gateway(m8: M8) -> None:
    assert m8.status()["verified"] is True
    m8.gateway.qwen_up = False  # only Qwen stops; the gateway and its public /health stay up
    down = m8.status()
    assert (down["verified"], down["authenticated"], down["error"]) == (
        False,
        False,
        "chat_server_down",
    )
    m8.gateway.qwen_up = True  # Qwen restarts
    back = m8.status()
    assert (back["verified"], back["authenticated"], back["error"]) == (True, True, None)
