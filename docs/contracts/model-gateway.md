# Model gateway contract (M2)

The backend knows one gateway **root** (`LLM_BASE_URL`, for example `https://<tunnel-host>`). It must not end in `/v1`; settings reject that, because the chat and embedding paths already contain it. Routes are composed as `root + path`:

| Method | Setting (default) | Served by | Backend method |
| --- | --- | --- | --- |
| `POST` | `MODEL_CHAT_PATH` (`/v1/chat/completions`) | vLLM via the gateway relay, streamed | `chat(messages, purpose, max_tokens, cancel)` |
| `POST` | `MODEL_EMBED_PATH` (`/v1/embeddings`) | BGE-M3 dense | `embed(texts, cancel)` (not called by M2) |
| `POST` | `MODEL_CHECK_PATH` (`/check`) | NLI checker | `check(pairs, cancel)` |
| `GET` | `MODEL_HEALTH_PATH` (`/health`) | gateway | `served_models()` |
| `GET` | `MODEL_READY_PATH` (`/ready`) | gateway → vLLM `/health` + `/v1/models` (token required) | `ready()` (M8 model status) |

Implementation: `backend/app/reporting/model_client.py`; gateway: `ml/model-gateway/gateway.py`.

## Requests

- **Chat**: OpenAI-compatible body with `model = MODEL_CHAT_NAME`, `temperature 0`, `stream true`, `chat_template_kwargs.enable_thinking = false`. The stream must end with a `finish_reason`; an `error` chunk or a missing finish reason is a failure. `finish_reason = length` is treated as malformed output.
- **Check**: `{"model", "pairs": [{"premise", "hypothesis"}]}` → `{"results": [{"entailment", "neutral", "contradiction"}], "model", "revision", "status"}`. Over-long pairs (> 512 checker tokens) are refused with HTTP 422, never truncated.
- **Embed**: `{"model", "input": [...]}` → OpenAI embedding list.
- **Health**: `{"models": {"chat": {"model", "revision"}, "embed": {...}, "checker": {...}}, "engine": {"gpu_memory_utilization", "max_model_len", ...}}`. The backend refuses to run unless the chat model and revision equal `MODEL_CHAT_NAME`/`MODEL_CHAT_REVISION` and the engine reports utilization `0.75` and context `4096` when present; a checker, if listed, must be the pinned one. The served payload is recorded in the run's diagnostics.

## Transport (M8)

`LLM_BASE_URL` must use `https` unless its host is loopback (`localhost`, `127.0.0.0/8`, `::1`) or a private-network address. The bearer token goes with every request, and settings refuse plain HTTP to anything else. The client never follows redirects (`follow_redirects=False`), and any 3xx is a `GatewayProtocolError`, so a redirect cannot carry the token to another host. `GET /ready` (token required) answers 200 with `{"chat": {"model", "revision", "ready": true}}` only when vLLM itself is up and serving the pinned chat model. Otherwise it answers 503 and the backend raises `GatewayChatServerDown` (`chat_server_down`).

## Service authentication and limits

`Authorization: Bearer $MODEL_GATEWAY_TOKEN` on every request (the gateway compares in constant time; `/health` is public and carries no content). The token is server-side only (`SecretStr`, never logged, never sent to the browser). The gateway receives only the prompt text, never database credentials, signing keys or session tokens, and vLLM listens on `127.0.0.1` behind it.

Timeouts: connect `MODEL_CONNECT_TIMEOUT_SECONDS` (5), per-chunk read `MODEL_READ_TIMEOUT_SECONDS` (60), whole-call deadline `MODEL_CALL_DEADLINE_SECONDS` (240; the measured 400-token report took 113.5 s).

## Cancellation

The workflow passes a `CancelToken`; a watcher thread sets it when `cancel_requested` is stored. The chat loop checks it per chunk and leaves the `httpx` stream context, closing the connection; the gateway then closes its upstream vLLM stream, which aborts the request (M1 measured `num_requests_running` returning to 0 within 0.28 s).

## Typed failures

| Exception | Cause | Run outcome |
| --- | --- | --- |
| `GatewayNotConfigured` | No `LLM_BASE_URL` or token in gateway mode | `failed`, `error: model_unavailable` |
| `GatewayUnavailable` | Connect error, 5xx, 404, 429, engine error chunk | same; during drafting only the narrative is withheld |
| `GatewayTimeout` | Connect/read timeout or deadline | same |
| `GatewayAuthError` | 401/403 | same |
| `GatewayProtocolError` | Malformed JSON, missing finish reason, wrong result count | same (checker: narrative withheld) |
| `GatewayRevisionMismatch` | Served model/revision/engine differs from the pinned configuration | `failed`, `model_unavailable` |
| `InferenceCancelled` | User cancelled | `cancelled` |

## Fake mode

`MODEL_MODE=fake` selects `FakeModelClient`, a deterministic double for development and tests. It is refused when `EAIOS_ENV=production`. `/ready` reports `model_inference: "fake"`; every run stores `inference_mode = fake`; every status event and the final verification carry `inference_label = "FAKE INFERENCE (deterministic development double, not model output)"`; the web UI shows that label. Fake runs are integration evidence only.
