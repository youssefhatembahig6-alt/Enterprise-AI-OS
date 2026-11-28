"""Model gateway client contract: route composition, service auth, typed failures,
streaming cancellation and pinned-revision checks (no network)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.reporting.model_client import (
    CancelToken,
    FakeModelClient,
    GatewayAuthError,
    GatewayModelClient,
    GatewayNotConfigured,
    GatewayProtocolError,
    GatewayRevisionMismatch,
    GatewayTimeout,
    GatewayUnavailable,
    InferenceCancelled,
    UnconfiguredModelClient,
    build_model_client,
)

ROOT = "https://gpu-gateway.example.test/eaios"
TOKEN = "service-token-for-tests-only"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "DATABASE_URL": "postgresql+psycopg://x@localhost/none",
        "REDIS_URL": "redis://localhost:6379/0",
        "QDRANT_URL": "http://localhost:6333",
        "S3_ENDPOINT_URL": "http://localhost:8333",
        "JWT_SECRET": "unit-test-secret-unit-test-secret-0000",
        "LLM_BASE_URL": ROOT,
        "MODEL_GATEWAY_TOKEN": TOKEN,
        "MODEL_READ_TIMEOUT_SECONDS": 2,
    }
    values.update(overrides)
    return Settings(**values)


def sse(chunks: list[dict[str, Any]]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def chunk(text: str | None = None, finish: str | None = None) -> dict[str, Any]:
    delta = {"content": text} if text is not None else {}
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


HEALTH = {
    "models": {
        "chat": {
            "model": "Qwen/Qwen3-8B-AWQ",
            "revision": "4da05a8edb55c6046cce958586c33b61da07bb79",
        },
        "embed": {"model": "BAAI/bge-m3", "revision": "5617a9f61b028005a4858fdac845db406aefb181"},
        "checker": {
            "model": "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
            "revision": "6f5cf0a2b59cabb106aca4c287eed12e357e90eb",
        },
    },
    "engine": {"gpu_memory_utilization": 0.75, "max_model_len": 4096},
}


def client_with(
    handler: Callable[[httpx.Request], httpx.Response], **kw: Any
) -> GatewayModelClient:
    return GatewayModelClient(settings(**kw), transport=httpx.MockTransport(handler))


def test_routes_are_composed_from_root_and_explicit_paths() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        path = request.url.path
        if path.endswith("/chat/completions"):
            return httpx.Response(200, content=sse([chunk("hi"), chunk(finish="stop")]))
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        if path.endswith("/check"):
            body = json.loads(request.content)
            assert body["pairs"] == [{"premise": "p", "hypothesis": "h"}]
            return httpx.Response(
                200, json={"results": [{"entailment": 0.9, "neutral": 0.05, "contradiction": 0.05}]}
            )
        return httpx.Response(200, json=HEALTH)

    client = client_with(handler)
    assert client.chat([{"role": "user", "content": "x"}], purpose="sql", max_tokens=5).text == "hi"
    assert client.embed(["q"]) == [[0.1, 0.2]]
    assert client.check([("p", "h")])[0].entailment == 0.9
    client.served_models()
    assert [u for _, u, _ in seen] == [
        f"{ROOT}/v1/chat/completions",
        f"{ROOT}/v1/embeddings",
        f"{ROOT}/check",
        f"{ROOT}/health",
    ]
    assert {auth for _, _, auth in seen} == {f"Bearer {TOKEN}"}


def test_chat_request_disables_thinking_and_streams() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=sse([chunk("a"), chunk("b", "stop")]))

    result = client_with(handler).chat(
        [{"role": "user", "content": "x"}], purpose="sql", max_tokens=7
    )
    assert result.text == "ab" and result.finish_reason == "stop"
    assert captured["stream"] is True and captured["max_tokens"] == 7
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["model"] == "Qwen/Qwen3-8B-AWQ" and captured["temperature"] == 0.0


def test_root_with_v1_is_rejected() -> None:
    with pytest.raises(ValueError, match="trailing /v1"):
        settings(LLM_BASE_URL="https://gateway.example.test/v1")


def test_unconfigured_gateway_fails_typed() -> None:
    client = build_model_client(settings(LLM_BASE_URL="", MODEL_GATEWAY_TOKEN=None))
    assert isinstance(client, UnconfiguredModelClient)
    with pytest.raises(GatewayNotConfigured):
        client.chat([], purpose="sql", max_tokens=1)
    with pytest.raises(GatewayNotConfigured):
        client.check([("a", "b")])


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, GatewayAuthError),
        (403, GatewayAuthError),
        (502, GatewayUnavailable),
        (503, GatewayUnavailable),
        (404, GatewayUnavailable),
        (400, GatewayProtocolError),
    ],
)
def test_http_status_is_typed(status: int, error: type[Exception]) -> None:
    client = client_with(lambda _: httpx.Response(status, json={}))
    with pytest.raises(error):
        client.check([("a", "b")])
    with pytest.raises(error):
        client.chat([{"role": "user", "content": "x"}], purpose="report", max_tokens=1)


def test_transport_failures_are_typed() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(GatewayUnavailable):
        client_with(refuse).check([("a", "b")])
    with pytest.raises(GatewayTimeout):
        client_with(slow).chat([{"role": "user", "content": "x"}], purpose="sql", max_tokens=1)


@pytest.mark.parametrize(
    "body",
    [
        b"data: not-json\n\n",
        sse([{"error": {"message": "CUDA out of memory"}}]),
        sse([chunk("partial")]),  # stream ends without finish_reason
    ],
)
def test_malformed_or_failed_streams_raise(body: bytes) -> None:
    client = client_with(lambda _: httpx.Response(200, content=body))
    with pytest.raises((GatewayProtocolError, GatewayUnavailable)):
        client.chat([{"role": "user", "content": "x"}], purpose="sql", max_tokens=5)


def _json_handler(body: Any) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return handler


def test_malformed_check_payloads_raise() -> None:
    payloads: list[dict[str, Any]] = [
        {"results": [{"entailment": "x"}]},
        {"nope": 1},
        {"results": []},
    ]
    for payload in payloads:
        with pytest.raises(GatewayProtocolError):
            client_with(_json_handler(payload)).check([("a", "b")])


class _SlowStream(httpx.SyncByteStream):
    """Streams chunks forever; the user cancels after a few have arrived."""

    def __init__(self, closed: threading.Event, token: CancelToken) -> None:
        self.closed = closed
        self.token = token
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        for i in range(10_000):
            self.sent += 1
            if self.sent == 5:
                self.token.cancel()
            yield f"data: {json.dumps(chunk(str(i)))}\n\n".encode()

    def close(self) -> None:
        self.closed.set()


def test_cancellation_closes_the_stream_mid_generation() -> None:
    closed = threading.Event()
    token = CancelToken()
    stream = _SlowStream(closed, token)
    client = client_with(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(InferenceCancelled):
        client.chat(
            [{"role": "user", "content": "x"}], purpose="report", max_tokens=5, cancel=token
        )
    assert closed.is_set(), "the HTTP stream must be closed so vLLM aborts the request"
    assert stream.sent < 50


def test_served_revisions_must_match_the_pinned_configuration() -> None:
    assert client_with(_json_handler(HEALTH)).served_models().raw == HEALTH
    changes: list[tuple[str, str, Any]] = [
        ("chat", "revision", "0" * 40),
        ("checker", "model", "other/checker"),
        ("engine", "gpu_memory_utilization", 0.85),
        ("engine", "max_model_len", 8192),
    ]
    for section, key, value in changes:
        health = json.loads(json.dumps(HEALTH))
        target = health["engine"] if section == "engine" else health["models"][section]
        target[key] = value
        with pytest.raises(GatewayRevisionMismatch):
            client_with(_json_handler(health)).served_models()


def test_fake_client_is_labelled_and_scriptable() -> None:
    fake = FakeModelClient({"sql": [GatewayUnavailable("down"), '{"sql": "SELECT 1"}']})
    assert fake.mode.value == "fake" and "FAKE INFERENCE" in fake.describe()["label"]
    with pytest.raises(GatewayUnavailable):
        fake.chat([{"role": "user", "content": "x"}], purpose="sql", max_tokens=1)
    assert fake.chat([{"role": "user", "content": "x"}], purpose="sql", max_tokens=1).text == (
        '{"sql": "SELECT 1"}'
    )


def test_redirects_are_refused_on_token_requests() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.example/steal"})

    client = client_with(handler)
    with pytest.raises(GatewayProtocolError):
        client.served_models()
    with pytest.raises(GatewayProtocolError):
        client.chat([{"role": "user", "content": "x"}], purpose="general", max_tokens=5)
    with pytest.raises(GatewayProtocolError):
        client.ready()
    assert all("elsewhere" not in u for u in seen)  # the redirect target was never contacted


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://gateway.example.org", True),
        ("http://127.0.0.1:8100", True),
        ("http://localhost:8100", True),
        ("http://172.19.0.1:18100", True),  # private network (container bridge)
        ("http://gateway.example.org", False),  # external host over plain HTTP
        ("http://8.8.8.8:8100", False),
        ("https://user:pw@gateway.example.org", False),
        ("https://gateway.example.org/?x=1", False),
    ],
)
def test_external_gateway_urls_must_use_https(url: str, ok: bool) -> None:
    if ok:
        assert settings(LLM_BASE_URL=url).model_gateway_url == url
    else:
        with pytest.raises(ValueError):
            settings(LLM_BASE_URL=url)
