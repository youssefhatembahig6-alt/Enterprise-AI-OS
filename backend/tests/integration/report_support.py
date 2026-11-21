"""Helpers for M2 report integration tests: raw SSE capture and leak assertions."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.conftest import DbHarness, login

# Exact NileTech figures in every spelling the stream or UI could use. A denied,
# abstained or failed run must contain none of them.
NILETECH_Q1 = {
    "order_count": 291,
    "net_sales": "80934385.00",
    "tax_amount": "11330813.90",
    "gross_sales": "92265198.90",
}
FORBIDDEN_FIGURES = (
    "80934385",
    "80,934,385",
    "11330813",
    "11,330,813",
    "92265198",
    "92,265,198",
    "593489",
    "593,489",
    "102987786",
    "102,987,786",
)


@dataclass
class Frame:
    id: int | None
    event: str
    data: dict[str, Any]


@dataclass
class Stream:
    raw: str
    frames: list[Frame]
    run_id: str | None
    status_code: int

    def events(self) -> list[str]:
        return [f.event for f in self.frames]

    def of(self, event: str) -> list[Frame]:
        return [f for f in self.frames if f.event == event]

    @property
    def done(self) -> dict[str, Any]:
        (frame,) = self.of("done")
        return frame.data


def parse_sse(raw: str) -> list[Frame]:
    frames = []
    for block in raw.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        fields: dict[str, str] = {}
        for line in lines:
            key, _, value = line.partition(": ")
            fields[key] = value
        if "event" in fields:
            frames.append(
                Frame(
                    id=int(fields["id"]) if "id" in fields else None,
                    event=fields["event"],
                    data=json.loads(fields["data"]),
                )
            )
    return frames


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ask(client: TestClient, token: str, question: str, **body: Any) -> Stream:
    with client.stream(
        "POST", "/api/v1/chat", json={"question": question, **body}, headers=auth(token)
    ) as response:
        raw = "".join(response.iter_text())
        return Stream(raw, parse_sse(raw), response.headers.get("x-run-id"), response.status_code)


def replay(
    client: TestClient,
    token: str,
    run_id: str,
    after: int = 0,
    headers: dict[str, str] | None = None,
) -> Stream:
    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/events",
        params={"after": after},
        headers={**auth(token), **(headers or {})},
    ) as response:
        raw = "".join(response.iter_text())
        return Stream(raw, parse_sse(raw), run_id, response.status_code)


def assert_no_figures(text_: str) -> None:
    for figure in FORBIDDEN_FIGURES:
        assert figure not in text_, f"leaked {figure}"


def assert_only_status_before_publication(stream: Stream) -> None:
    """Content events appear only after the publication status, never before."""

    seen_publication = False
    for frame in stream.frames:
        if frame.event == "status" and frame.data.get("step") == "publication":
            seen_publication = True
        if frame.event in ("token", "citation"):
            assert seen_publication, f"{frame.event} before publication"
        if not seen_publication:
            assert frame.event in ("status", "error", "done"), frame.event


def wait_for_run(db: DbHarness, run_id: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.owner_engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM report_runs WHERE id = :id"), {"id": run_id}
            ).scalar_one()
        if status not in ("queued", "running"):
            return str(status)
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def token_for(client: TestClient, db: DbHarness, ref: str) -> str:
    return login(client, db, ref)
