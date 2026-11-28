"""M8 Colab runner: a browser check passes only on its own fresh, passing evidence.

``ml/colab-m8-chat/run_m8_chat_colab.py`` runs ``scripts/m8_chat_ui_check.py`` as a child
process. Every run must write into a new folder, and the runner accepts it only when the
child exits 0 AND the report in that folder carries this run's id and says pass. The key
regression: a rerun that fails (for example a crash before any report is written) must not
be judged by a passing report an earlier run left behind.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
RUNNER = REPO / "ml" / "colab-m8-chat" / "run_m8_chat_colab.py"
REPORT = "m8-chat-ui-check.json"

Child = Callable[[Path, str], int]


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("M3_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("EAIOS_REPO", str(REPO))
    saved = {k: sys.modules.get(k) for k in ("run_m3_colab", "run_m8_chat_colab")}
    for k in saved:
        sys.modules.pop(k, None)
    spec = importlib.util.spec_from_file_location("run_m8_chat_colab", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_m8_chat_colab"] = module
    try:
        spec.loader.exec_module(module)
        module.STEPS.clear()
        yield module
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if str(RUNNER.parents[1] / "colab-m3") in sys.path:
            sys.path.remove(str(RUNNER.parents[1] / "colab-m3"))
    assert os.environ["M3_RUN_DIR"] == str(tmp_path / "run")


def fake_child(monkeypatch: pytest.MonkeyPatch, runner: ModuleType, child: Child) -> list[Path]:
    """Replace the browser-check process; ``child(out, run_id)`` returns its exit status."""

    outs: list[Path] = []

    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--out") + 1])
        run_id = cmd[cmd.index("--run-id") + 1]
        assert out.is_dir() and not any(out.iterdir()), "check must start in an empty folder"
        outs.append(out)
        return subprocess.CompletedProcess(cmd, child(out, run_id), "", "")

    monkeypatch.setattr(runner.subprocess, "run", run)
    return outs


def write_report(out: Path, run_id: str, passed: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT).write_text(json.dumps({"run_id": run_id, "pass": passed, "checks": {}}))


def reports(passed: bool, exit_status: int, run_id: str | None = None) -> Child:
    """A child that writes a report (for this run, or ``run_id``) and exits ``exit_status``."""

    def child(out: Path, this_run: str) -> int:
        write_report(out, run_id or this_run, passed)
        return exit_status

    return child


def writes_garbage(out: Path, run_id: str) -> int:
    (out / REPORT).write_text("{not json")
    return 0


def plant_old_passing_reports(runner: ModuleType, name: str) -> None:
    out_root = runner.m3.OUT / name
    write_report(out_root, "old-run", True)  # the former fixed location
    write_report(
        out_root / "20261127T235146000000Z-0ld0ld00", "20261127T235146000000Z-0ld0ld00", True
    )


@pytest.mark.parametrize("name", ["ui", "preview"])
def test_failed_rerun_with_an_old_passing_report_fails(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    plant_old_passing_reports(runner, name)
    outs = fake_child(monkeypatch, runner, lambda out, run_id: 1)  # crashed: no report

    result = runner.ui_check(Path("python"), name=name)

    assert result["pass"] is False
    assert "browser check exited 1" in result["reasons"]
    assert f"no {REPORT} from this run" in result["reasons"]
    assert runner.STEPS[f"{name}_check"]["exit"] == 1
    assert runner.STEPS[f"{name}_check"]["pass"] is False
    # The old evidence is untouched and was not read as this run's.
    assert outs[0].parent == runner.m3.OUT / name and outs[0].name == result["run_id"]
    assert (runner.m3.OUT / name / REPORT).is_file()


def test_failed_rerun_fails_even_with_a_passing_report_of_its_own(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_child(monkeypatch, runner, reports(passed=True, exit_status=1))

    result = runner.ui_check(Path("python"))

    assert result["pass"] is False
    assert result["reasons"] == ["browser check exited 1"]


def test_exit_zero_with_a_report_from_another_run_fails(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_child(monkeypatch, runner, reports(passed=True, exit_status=0, run_id="old-run"))

    result = runner.ui_check(Path("python"))

    assert result["pass"] is False
    assert result["reasons"] == [f"{REPORT} is not from this run"]


@pytest.mark.parametrize(
    ("child", "reason"),
    [
        (lambda out, run_id: 0, f"no {REPORT} from this run"),
        (reports(passed=False, exit_status=0), f"{REPORT} reports failure"),
        (
            writes_garbage,
            f"{REPORT} is unreadable",
        ),
    ],
)
def test_exit_zero_without_a_passing_report_of_this_run_fails(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch, child: Child, reason: str
) -> None:
    fake_child(monkeypatch, runner, child)

    result = runner.ui_check(Path("python"))

    assert result["pass"] is False
    assert result["reasons"] == [reason]


def test_exit_zero_and_this_runs_passing_report_passes_in_a_fresh_folder_each_time(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    plant_old_passing_reports(runner, "preview")
    outs = fake_child(monkeypatch, runner, reports(passed=True, exit_status=0))

    first = runner.ui_check(Path("python"), name="preview")
    second = runner.ui_check(Path("python"), name="preview")

    assert first["pass"] is True and second["pass"] is True
    assert first["reasons"] == [] and second["reasons"] == []
    assert first["run_id"] != second["run_id"]
    assert outs[0] != outs[1]
    assert second["evidence"] == f"preview/{second['run_id']}/{REPORT}"
    assert runner.STEPS["preview_check"] == {
        "run_id": second["run_id"],
        "exit": 0,
        "evidence": second["evidence"],
        "pass": True,
        "reasons": [],
    }
