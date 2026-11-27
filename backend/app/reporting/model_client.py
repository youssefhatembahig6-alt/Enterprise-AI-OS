"""Model gateway client with explicit chat, embed and check methods.

Route composition (docs/contracts/model-gateway.md):

    chat   POST {LLM_BASE_URL}{MODEL_CHAT_PATH}    default /v1/chat/completions (vLLM, streamed)
    embed  POST {LLM_BASE_URL}{MODEL_EMBED_PATH}   default /v1/embeddings      (BGE-M3)
    check  POST {LLM_BASE_URL}{MODEL_CHECK_PATH}   default /check              (NLI checker)
    health GET  {LLM_BASE_URL}{MODEL_HEALTH_PATH}  default /health             (served revisions)

Every call sends ``Authorization: Bearer <MODEL_GATEWAY_TOKEN>`` (server-side only),
uses a connect timeout, a per-chunk read timeout and a whole-call deadline, and can be
cancelled through a ``CancelToken``. A cancelled chat closes the HTTP stream, which
makes vLLM abort the request (measured in M1). Failures are typed.

``FakeModelClient`` is an explicit deterministic double. Its ``mode`` is ``"fake"``;
the workflow stores that on the run and every event, and the UI labels it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.config import ModelMode, Settings

Purpose = Literal["router", "sql", "report", "answer", "general"]


class ModelError(Exception):
    code = "model_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}: {detail}" if detail else self.code)
        self.detail = detail


class GatewayNotConfigured(ModelError):
    code = "not_configured"


class GatewayUnavailable(ModelError):
    code = "unavailable"


class GatewayTimeout(ModelError):
    code = "timeout"


class GatewayAuthError(ModelError):
    code = "auth_failed"


class GatewayProtocolError(ModelError):
    code = "protocol_error"


class GatewayRevisionMismatch(ModelError):
    code = "revision_mismatch"


class InferenceCancelled(ModelError):
    code = "cancelled"


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise InferenceCancelled


@dataclass(frozen=True, slots=True)
class ChatResult:
    text: str
    finish_reason: str
    completion_tokens: int | None
    ttft_seconds: float | None
    total_seconds: float


@dataclass(frozen=True, slots=True)
class CheckResult:
    entailment: float
    neutral: float
    contradiction: float


@dataclass
class ServedModels:
    """What the gateway reports it is serving; recorded on every real run."""

    raw: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return self.raw


@dataclass(frozen=True, slots=True)
class TextEmbedding:
    """Dense vector plus BGE-M3 style sparse lexical weights (token id -> weight)."""

    dense: list[float]
    sparse: dict[int, float]


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """Identity of the vectors a client produces; chunks are only comparable within one."""

    model: str
    revision: str
    dense_size: int
    fake: bool

    @property
    def slug(self) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", f"{self.model}_{self.revision[:12]}".lower())
        return f"{base.strip('_')}_{self.dense_size}"


class ModelClient(Protocol):
    mode: ModelMode

    def describe(self) -> dict[str, Any]: ...

    def served_models(self, cancel: CancelToken | None = None) -> ServedModels: ...

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: Purpose,
        max_tokens: int,
        cancel: CancelToken | None = None,
    ) -> ChatResult: ...

    def embed(
        self, texts: list[str], *, cancel: CancelToken | None = None
    ) -> list[list[float]]: ...

    def embedding_space(self) -> EmbeddingSpace: ...

    def embed_text(
        self, texts: list[str], *, cancel: CancelToken | None = None
    ) -> list[TextEmbedding]: ...

    def check(
        self, pairs: list[tuple[str, str]], *, cancel: CancelToken | None = None
    ) -> list[CheckResult]: ...

    def parse_document(self, filename: str, data: bytes) -> dict[str, Any]: ...


def _json_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GatewayProtocolError("response is not JSON") from exc


class GatewayModelClient:
    mode = ModelMode.GATEWAY

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.model_gateway_url:
            raise GatewayNotConfigured("LLM_BASE_URL is not set")
        if settings.model_gateway_token is None:
            raise GatewayNotConfigured("MODEL_GATEWAY_TOKEN is not set")
        self._s = settings
        self._root = settings.model_gateway_url
        self._deadline = settings.model_call_deadline_seconds
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.model_connect_timeout_seconds,
                read=settings.model_read_timeout_seconds,
                write=10.0,
                pool=5.0,
            ),
            headers={
                "Authorization": f"Bearer {settings.model_gateway_token.get_secret_value()}",
                "User-Agent": "eaios-backend",
            },
            follow_redirects=False,
            transport=transport,
        )

    def url(self, path: str) -> str:
        return f"{self._root}{path}"

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "chat": {"model": self._s.model_chat_name, "revision": self._s.model_chat_revision},
            "embed": {"model": self._s.model_embed_name, "revision": self._s.model_embed_revision},
            "checker": {
                "model": self._s.model_checker_name,
                "revision": self._s.model_checker_revision,
                "status": "development_baseline",
            },
            "expected": {
                "gpu_memory_utilization": self._s.model_expected_gpu_memory_utilization,
                "max_model_len": self._s.model_expected_max_model_len,
            },
        }

    @contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except httpx.TimeoutException as exc:
            raise GatewayTimeout(type(exc).__name__) from exc
        except httpx.TransportError as exc:
            raise GatewayUnavailable(type(exc).__name__) from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise GatewayAuthError(str(response.status_code))
        if response.status_code >= 500 or response.status_code in (404, 429):
            raise GatewayUnavailable(str(response.status_code))
        if response.status_code >= 400:
            raise GatewayProtocolError(str(response.status_code))

    def _post(self, path: str, body: dict[str, Any], cancel: CancelToken | None) -> Any:
        if cancel:
            cancel.raise_if_cancelled()
        with self._errors():
            response = self._client.post(self.url(path), json=body, timeout=self._deadline)
        self._raise_for_status(response)
        if cancel:
            cancel.raise_if_cancelled()
        return _json_body(response)

    def served_models(self, cancel: CancelToken | None = None) -> ServedModels:
        """Fetch served revisions and refuse to run against anything but the pinned set."""

        if cancel:
            cancel.raise_if_cancelled()
        with self._errors():
            response = self._client.get(self.url(self._s.model_health_path))
        self._raise_for_status(response)
        body = _json_body(response)
        if not isinstance(body, dict) or not isinstance(body.get("models"), dict):
            raise GatewayProtocolError("health response has no models")
        models = body["models"]
        # The chat model must be the pinned one. The checker may be absent (narrative
        # is then withheld), but a different checker is never accepted.
        expected = {
            "chat": (self._s.model_chat_name, self._s.model_chat_revision, True),
            "checker": (self._s.model_checker_name, self._s.model_checker_revision, False),
        }
        for key, (name, revision, required) in expected.items():
            served = models.get(key)
            if served is None and not required:
                continue
            served = served or {}
            if served.get("model") != name or served.get("revision") != revision:
                raise GatewayRevisionMismatch(f"{key} is not {name}@{revision[:12]}")
        engine = body.get("engine") or {}
        util = engine.get("gpu_memory_utilization")
        if (
            util is not None
            and abs(float(util) - self._s.model_expected_gpu_memory_utilization) > 1e-9
        ):
            raise GatewayRevisionMismatch(f"gpu_memory_utilization {util}")
        max_len = engine.get("max_model_len")
        if max_len is not None and int(max_len) != self._s.model_expected_max_model_len:
            raise GatewayRevisionMismatch(f"max_model_len {max_len}")
        return ServedModels(raw=body)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: Purpose,
        max_tokens: int,
        cancel: CancelToken | None = None,
    ) -> ChatResult:
        body = {
            "model": self._s.model_chat_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": True,
            "stream_options": {"include_usage": True},
            # Qwen3: thinking off for agents (specification section 6).
            "chat_template_kwargs": {"enable_thinking": False},
        }
        started = time.monotonic()
        ttft: float | None = None
        parts: list[str] = []
        finish: str | None = None
        tokens: int | None = None
        if cancel:
            cancel.raise_if_cancelled()
        with (
            self._errors(),
            self._client.stream("POST", self.url(self._s.model_chat_path), json=body) as response,
        ):
            self._raise_for_status(response)
            for line in response.iter_lines():
                if cancel and cancel.cancelled:
                    raise InferenceCancelled(f"{purpose} stream closed")
                if time.monotonic() - started > self._deadline:
                    raise GatewayTimeout(f"{purpose} deadline")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise GatewayProtocolError("bad stream chunk") from exc
                if "error" in chunk:
                    raise GatewayUnavailable("engine error event in stream")
                if chunk.get("usage"):
                    tokens = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        if ttft is None:
                            ttft = time.monotonic() - started
                        parts.append(delta)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        if finish is None:
            raise GatewayProtocolError("stream ended without finish_reason")
        return ChatResult(
            text="".join(parts),
            finish_reason=finish,
            completion_tokens=tokens,
            ttft_seconds=ttft,
            total_seconds=time.monotonic() - started,
        )

    def embed(self, texts: list[str], *, cancel: CancelToken | None = None) -> list[list[float]]:
        body = self._post(
            self._s.model_embed_path, {"model": self._s.model_embed_name, "input": texts}, cancel
        )
        try:
            return [list(map(float, item["embedding"])) for item in body["data"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayProtocolError("malformed embeddings") from exc

    def embedding_space(self) -> EmbeddingSpace:
        return EmbeddingSpace(
            self._s.model_embed_name, self._s.model_embed_revision, BGE_M3_DENSE_SIZE, False
        )

    def embed_text(
        self, texts: list[str], *, cancel: CancelToken | None = None
    ) -> list[TextEmbedding]:
        """BGE-M3 dense and sparse vectors in one call (``return_sparse``)."""

        out: list[TextEmbedding] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            body = self._post(
                self._s.model_embed_path,
                {"model": self._s.model_embed_name, "input": batch, "return_sparse": True},
                cancel,
            )
            try:
                items = body["data"]
                for item in items:
                    dense = [float(v) for v in item["embedding"]]
                    sparse_raw = item["sparse"]
                    sparse = {
                        int(i): float(w)
                        for i, w in zip(sparse_raw["indices"], sparse_raw["values"], strict=True)
                    }
                    if len(dense) != BGE_M3_DENSE_SIZE:
                        raise GatewayProtocolError("unexpected embedding size")
                    out.append(TextEmbedding(dense, sparse))
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayProtocolError("malformed embeddings") from exc
            if len(items) != len(batch):
                raise GatewayProtocolError("embedding count mismatch")
        return out

    def check(
        self, pairs: list[tuple[str, str]], *, cancel: CancelToken | None = None
    ) -> list[CheckResult]:
        body = self._post(
            self._s.model_check_path,
            {
                "model": self._s.model_checker_name,
                "pairs": [{"premise": p, "hypothesis": h} for p, h in pairs],
            },
            cancel,
        )
        try:
            results = [
                CheckResult(
                    entailment=float(r["entailment"]),
                    neutral=float(r["neutral"]),
                    contradiction=float(r["contradiction"]),
                )
                for r in body["results"]
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayProtocolError("malformed check results") from exc
        if len(results) != len(pairs):
            raise GatewayProtocolError("check result count mismatch")
        return results

    def parse_document(self, filename: str, data: bytes) -> dict[str, Any]:
        """Docling on the gateway: canonical elements for a PDF (layout, tables, OCR)."""

        with self._errors():
            response = self._client.post(
                self.url(self._s.model_parse_path),
                json={"filename": filename, "content_base64": base64.b64encode(data).decode()},
                timeout=self._s.docling_timeout_seconds,
            )
        self._raise_for_status(response)
        body = _json_body(response)
        if not isinstance(body, dict) or "elements" not in body:
            raise GatewayProtocolError("malformed parse result")
        return body


# --- explicit fake ---------------------------------------------------------------

BGE_M3_DENSE_SIZE = 1024
EMBED_BATCH = 32
FAKE_DENSE_SIZE = 256
_WORD = re.compile(r"\w+", re.UNICODE)


def _feature_index(feature: str, modulo: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % modulo, 1.0 if (value >> 63) & 1 else -1.0


def fake_embedding(text: str) -> TextEmbedding:
    """Deterministic feature-hashing vectors (words and character trigrams). Lexical
    similarity only: a development/test double for BGE-M3, never model evidence."""

    words = [w.lower() for w in _WORD.findall(text)]
    dense = [0.0] * FAKE_DENSE_SIZE
    for word in words:
        index, sign = _feature_index("w:" + word, FAKE_DENSE_SIZE)
        dense[index] += sign
        padded = f"#{word}#"
        for i in range(len(padded) - 2):
            index, sign = _feature_index("t:" + padded[i : i + 3], FAKE_DENSE_SIZE)
            dense[index] += 0.5 * sign
    norm = math.sqrt(sum(v * v for v in dense)) or 1.0
    sparse: dict[int, float] = {}
    for word in words:
        index, _ = _feature_index("s:" + word, 2**31 - 1)
        sparse[index] = sparse.get(index, 0.0) + 1.0
    return TextEmbedding(
        [v / norm for v in dense], {k: 1.0 + math.log(v) for k, v in sparse.items()}
    )


FAKE_LABEL = "FAKE INFERENCE (deterministic development double, not model output)"

ScriptStep = str | Exception | Callable[[list[dict[str, str]]], str]


class FakeModelClient:
    """Deterministic double. Either scripted per purpose (tests) or a simple default
    that writes the canonical query for the intent in the prompt (development demo).
    """

    mode = ModelMode.FAKE

    def __init__(
        self,
        script: dict[str, list[ScriptStep]] | None = None,
        *,
        checker: Callable[[str, str], CheckResult] | Exception | None = None,
        checker_available: bool = True,
        embed_error: Exception | None = None,
        parse_result: dict[str, Any] | Exception | None = None,
    ) -> None:
        self._embed_error = embed_error
        self._parse_result = parse_result
        self._script = {k: list(v) for k, v in (script or {}).items()}
        self._checker = checker
        self._checker_available = checker_available
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self._lock = threading.Lock()

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "label": FAKE_LABEL}

    def served_models(self, cancel: CancelToken | None = None) -> ServedModels:
        return ServedModels(raw={"fake": True, "label": FAKE_LABEL})

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: Purpose,
        max_tokens: int,
        cancel: CancelToken | None = None,
    ) -> ChatResult:
        if cancel:
            cancel.raise_if_cancelled()
        with self._lock:
            self.calls.append((purpose, messages))
            queue = self._script.get(purpose)
            step: ScriptStep | None = queue.pop(0) if queue else None
        if step is None:
            text = _default_fake(purpose, messages)
        elif isinstance(step, Exception):
            raise step
        elif callable(step):
            text = step(messages)
        else:
            text = step
        if cancel:
            cancel.raise_if_cancelled()
        return ChatResult(
            text=text,
            finish_reason="stop",
            completion_tokens=None,
            ttft_seconds=0.0,
            total_seconds=0.0,
        )

    def embed(self, texts: list[str], *, cancel: CancelToken | None = None) -> list[list[float]]:
        return [e.dense for e in self.embed_text(texts, cancel=cancel)]

    def embedding_space(self) -> EmbeddingSpace:
        return EmbeddingSpace("fake-feature-hash", "v1", FAKE_DENSE_SIZE, True)

    def embed_text(
        self, texts: list[str], *, cancel: CancelToken | None = None
    ) -> list[TextEmbedding]:
        if cancel:
            cancel.raise_if_cancelled()
        if self._embed_error is not None:
            raise self._embed_error
        return [fake_embedding(t) for t in texts]

    def check(
        self, pairs: list[tuple[str, str]], *, cancel: CancelToken | None = None
    ) -> list[CheckResult]:
        if not self._checker_available:
            raise GatewayUnavailable("fake checker disabled")
        if isinstance(self._checker, Exception):
            raise self._checker
        if self._checker is not None:
            return [self._checker(p, h) for p, h in pairs]
        return [CheckResult(entailment=0.99, neutral=0.005, contradiction=0.005) for _ in pairs]

    def parse_document(self, filename: str, data: bytes) -> dict[str, Any]:
        """No Docling in fake mode unless a test provides a canned result; ingestion then
        uses the documented local fallback."""

        if isinstance(self._parse_result, Exception):
            raise self._parse_result
        if self._parse_result is None:
            raise GatewayNotConfigured("no Docling in fake mode")
        return self._parse_result


def _fake_answer(prompt: str) -> str:
    """Quote the sentence of the first source that best overlaps the question."""

    question = re.search(r"^Question: (.+)$", prompt, flags=re.MULTILINE)
    terms = {
        t.lower()[:6] for t in _WORD.findall(question.group(1) if question else "") if len(t) > 3
    }
    for match in re.finditer(r'<source id="(s\d+)"[^\n]*>\n(.*?)\n</source>', prompt, flags=re.S):
        source_id, body = match.group(1), match.group(2)
        best, best_score = "", 0
        for raw_sentence in re.split(r"(?<=[.!?])\s+|\n", body):
            sentence = raw_sentence.strip()
            score = len(terms & {t.lower()[:6] for t in _WORD.findall(sentence) if len(t) > 3})
            if score > best_score and len(sentence) >= 8:
                best, best_score = sentence[:400], score
        if best:
            return json.dumps(
                {
                    "answerable": True,
                    "sentences": [{"text": best, "cites": [source_id], "quote": best}],
                }
            )
    return json.dumps({"answerable": False})


def _default_fake(purpose: Purpose, messages: list[dict[str, str]]) -> str:
    prompt = messages[-1]["content"] if purpose != "answer" else messages[1]["content"]
    if purpose == "general":
        return (
            "FAKE general reply (deterministic development double, not model output). "
            f"You asked: {prompt.strip()[:200]}"
        )
    if purpose == "answer":
        return _fake_answer(prompt)
    if purpose == "router":
        return json.dumps({"route": "sales_report"})
    if purpose == "sql":
        match = re.search(
            r"order_date from (\d{4}-\d{2}-\d{2}) through (\d{4}-\d{2}-\d{2})", prompt
        )
        statuses = re.search(r"Included order statuses: ([A-Z_, ]+)", prompt)
        start, end = (match.group(1), match.group(2)) if match else ("1900-01-01", "1900-01-01")
        status_list = ", ".join(
            f"'{s.strip()}'" for s in (statuses.group(1) if statuses else "FULFILLED").split(",")
        )
        sql = (
            "SELECT count(*) AS order_count, sum(o.subtotal) AS net_sales,"
            " sum(o.tax) AS tax_amount, sum(o.total) AS gross_sales"
            " FROM reporting.sales_orders o"
            f" WHERE o.order_date >= DATE '{start}' AND o.order_date <= DATE '{end}'"
            f" AND o.status IN ({status_list})"
        )
        return json.dumps({"sql": sql})
    figures = dict(re.findall(r"^- (\w+): (.+)$", prompt, flags=re.MULTILINE))
    label = re.search(r"Period: (.+)$", prompt, flags=re.MULTILINE)
    period = label.group(1).strip() if label else "the period"
    return json.dumps(
        {
            "sentences": [
                {
                    "text": (
                        f"In {period}, net sales (tax-exclusive) were "
                        f"{figures.get('net_sales', '?')} from {figures.get('order_count', '?')}"
                        " fulfilled orders."
                    ),
                    "cites": ["net_sales", "order_count"],
                },
                {
                    "text": (
                        f"Tax was {figures.get('tax_amount', '?')}, giving gross sales of "
                        f"{figures.get('gross_sales', '?')}."
                    ),
                    "cites": ["tax_amount", "gross_sales"],
                },
            ]
        }
    )


class UnconfiguredModelClient:
    """Gateway mode without a configured gateway: every call fails clearly and typed,
    so runs end with ``model_unavailable`` instead of falling back to anything."""

    mode = ModelMode.GATEWAY

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "configured": False, "reason": self._reason}

    def served_models(self, cancel: CancelToken | None = None) -> ServedModels:
        raise GatewayNotConfigured(self._reason)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: Purpose,
        max_tokens: int,
        cancel: CancelToken | None = None,
    ) -> ChatResult:
        raise GatewayNotConfigured(self._reason)

    def embed(self, texts: list[str], *, cancel: CancelToken | None = None) -> list[list[float]]:
        raise GatewayNotConfigured(self._reason)

    def embedding_space(self) -> EmbeddingSpace:
        return EmbeddingSpace("unconfigured", "none", BGE_M3_DENSE_SIZE, False)

    def embed_text(
        self, texts: list[str], *, cancel: CancelToken | None = None
    ) -> list[TextEmbedding]:
        raise GatewayNotConfigured(self._reason)

    def check(
        self, pairs: list[tuple[str, str]], *, cancel: CancelToken | None = None
    ) -> list[CheckResult]:
        raise GatewayNotConfigured(self._reason)

    def parse_document(self, filename: str, data: bytes) -> dict[str, Any]:
        raise GatewayNotConfigured(self._reason)


def build_model_client(settings: Settings) -> ModelClient:
    if settings.model_mode is ModelMode.FAKE:
        return FakeModelClient()
    try:
        return GatewayModelClient(settings)
    except GatewayNotConfigured as exc:
        return UnconfiguredModelClient(exc.detail)
