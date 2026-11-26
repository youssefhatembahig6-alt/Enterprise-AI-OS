"""M7 functional acceptance tooling: the safety checks in the disposable-stack wrapper
(scripts/m7_acceptance.py) and the Colab real-model smoke validator
(ml/colab-m7-acceptance/run_m7_smoke_colab.py). Pure functions only; no Docker, no
network. Skipped where the repository root is not present (the backend test image).
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "m7_acceptance.py"
SMOKE = ROOT / "ml" / "colab-m7-acceptance" / "run_m7_smoke_colab.py"
REAL = (
    ROOT / "docs/evidence/m7-functional/2026-11-26-colab-real-model-smoke/m6-demo-real-model.json"
)
FAKE = ROOT / "docs/evidence/m7-functional/2026-11-26-m6-demo-compose-fake-model.json"

pytestmark = pytest.mark.skipif(
    not (WRAPPER.exists() and SMOKE.exists()), reason="repository root not available"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def wrapper() -> ModuleType:
    return _load("m7_acceptance", WRAPPER)


@pytest.fixture(scope="module")
def smoke(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    os.environ.setdefault("M3_RUN_DIR", str(tmp_path_factory.mktemp("m7-run")))
    sys.path.insert(0, str(SMOKE.parent.parent / "colab-m3"))
    return _load("run_m7_smoke_colab", SMOKE)


# -- 1. the wrapper never leaves the eaios-m7acc project ---------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "eaios", "config"],
        ["--project-name", "eaios", "up"],
        ["--project-name=eaios", "down"],
        ["-peaios", "ps"],
        ["-f", "compose.yaml", "config"],
        ["--file=compose.yaml", "config"],
        ["--env-file", ".env", "up"],
        ["--project-directory", "../other", "up"],
        ["--profile", "test", "-p", "eaios", "run", "backend-tests"],
    ],
)
def test_passthrough_refuses_project_and_file_overrides(
    wrapper: ModuleType, args: list[str]
) -> None:
    assert wrapper.forbidden_globals(args)


@pytest.mark.parametrize(
    "args",
    [
        ["--profile", "test", "run", "--rm", "-v", "a:/b", "backend-tests", "pytest"],
        ["run", "-p", "8080:80", "api"],  # after the subcommand, -p publishes a port
        ["config", "--services"],
    ],
)
def test_passthrough_allows_ordinary_commands(wrapper: ModuleType, args: list[str]) -> None:
    assert wrapper.forbidden_globals(args) == []


def test_base_pins_the_project_and_drops_inherited_compose_variables(
    wrapper: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "eaios")
    monkeypatch.setenv("COMPOSE_FILE", "compose.yaml")
    monkeypatch.delenv("DOCKER_COMPOSE", raising=False)
    cmd = wrapper.base()
    assert cmd[cmd.index("-p") + 1] == "eaios-m7acc"
    env = wrapper.child_env()
    assert "COMPOSE_PROJECT_NAME" not in env and "COMPOSE_FILE" not in env


def test_docker_compose_override_cannot_carry_a_project(
    wrapper: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_COMPOSE", "docker compose -p eaios")
    with pytest.raises(SystemExit):
        wrapper.base()


# -- 2. seed-legal succeeds only when every fixture is ready and searchable --------------

TITLES = ["Aswan", "Red Sea", "NileCloud"]


def _ok(title: str) -> dict[str, Any]:
    return {"title": title, "status": "ready", "searchable": True}


@pytest.mark.parametrize(
    ("docs", "waiting", "problems"),
    [
        ([_ok(t) for t in TITLES], 0, 0),
        ([_ok("Aswan")], 0, 2),  # a ready subset is not success
        ([_ok("Aswan"), _ok("Red Sea"), {"title": "NileCloud", "status": "failed"}], 0, 1),
        ([_ok("Aswan"), _ok("Red Sea"), {"title": "NileCloud", "status": "needs_ocr"}], 0, 1),
        ([_ok("Aswan"), _ok("Aswan"), _ok("Red Sea"), _ok("NileCloud")], 0, 1),
        ([_ok("Aswan"), {"title": "Red Sea", "status": "indexing"}, _ok("NileCloud")], 1, 0),
        (
            [_ok("Aswan"), _ok("Red Sea"), {"title": "NileCloud", "status": "ready"}],
            1,
            0,
        ),  # bug B2 window: ready but not yet searchable keeps waiting
        ([], 0, 3),
    ],
)
def test_legal_fixture_state(
    wrapper: ModuleType, docs: list[dict[str, Any]], waiting: int, problems: int
) -> None:
    got_waiting, got_problems = wrapper.legal_fixture_state(TITLES, docs)
    assert (len(got_waiting), len(got_problems)) == (waiting, problems)


# -- 3. the Colab validator needs an explicit gateway label on done and every status -----


def _events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = json.loads(path.read_text())["report"]["events"]
    return events


def test_saved_real_model_evidence_passes(smoke: ModuleType) -> None:
    assert smoke.inference_problems(_events(REAL)) == []


def test_saved_fake_model_evidence_fails(smoke: ModuleType) -> None:
    assert smoke.inference_problems(_events(FAKE))


def _strip(events: list[dict[str, Any]], keep: int | None) -> list[dict[str, Any]]:
    """Remove the inference label from every status/done event except index ``keep``."""
    out = copy.deepcopy(events)
    for i, event in enumerate(out):
        if event["event"] in ("status", "done") and i != keep:
            event["data"].pop("inference", None)
    return out


def test_one_labelled_event_cannot_vouch_for_the_rest(smoke: ModuleType) -> None:
    events = _events(REAL)
    first_status = next(i for i, e in enumerate(events) if e["event"] == "status")
    assert smoke.inference_problems(_strip(events, keep=first_status))
    assert smoke.inference_problems(_strip(events, keep=len(events) - 1))


def test_missing_done_label_or_mode_fails(smoke: ModuleType) -> None:
    events = copy.deepcopy(_events(REAL))
    events[-1]["data"]["verification"].pop("inference_mode")
    assert smoke.inference_problems(events)


def test_stream_shape_is_required(smoke: ModuleType) -> None:
    events = _events(REAL)
    assert smoke.inference_problems([])
    assert smoke.inference_problems(events[:-1])  # no done
    assert smoke.inference_problems([*events, events[0]])  # done not last
    assert smoke.inference_problems([e for e in events if e["event"] != "status"])


def test_token_and_citation_may_be_unlabelled_but_never_fake(smoke: ModuleType) -> None:
    events = copy.deepcopy(_events(REAL))
    assert all("inference" not in e["data"] for e in events if e["event"] == "token")
    next(e for e in events if e["event"] == "token")["data"]["inference"] = "fake"
    assert smoke.inference_problems(events)
