from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import crewplane.cli.app as cli
from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.artifacts.manager import OutputManager
from crewplane.observability import ObservabilityHub
from tests.integration.cli.repeat_force_run_support import create_project
from tests.integration.cli.test_workflow_runner_terminal_recovery import (
    RequiredStopFailureHub,
)
from tests.integration.cli.workflow_runner_support import run_directories
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    provider_failure,
)


@pytest.mark.parametrize(
    "failure", ["execution", "components", "cleanup", "allocation", "publication"]
)
def test_failure_in_complete_run_stops_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    if failure == "execution":
        target = patch.object(
            cli, "execute_workflow", side_effect=RuntimeError("execution failed")
        )
    elif failure == "components":
        target = patch(
            "crewplane.cli.run.execution.build_components_for_run",
            side_effect=RuntimeError("component setup failed"),
        )
    elif failure == "cleanup":
        target = patch.object(cli, "ObservabilityHub", new=RequiredStopFailureHub)
    elif failure == "allocation":
        target = patch(
            "crewplane.cli.run.execution.allocate_run_output",
            side_effect=OSError("allocation failed"),
        )
    else:
        target = patch.object(
            OutputManager,
            "update_run_manifest_status",
            side_effect=OSError("publication failed"),
        )
    with target:
        result = project.run("--no-live")
    assert result.exit_code == 1, result.output
    assert "Run 1 of 3 (fresh)" in result.output
    assert "Run 2 of 3" not in result.output
    assert "Invalid:" not in result.output
    assert len(run_directories(tmp_path)) == (0 if failure == "allocation" else 1)
    manifests = project.manifests()
    incomplete = failure in {"cleanup", "publication"}
    if manifests:
        assert manifests[0]["status"] == ("running" if incomplete else "failed")
    locks = list((tmp_path / ".crewplane/locks").iterdir())
    assert bool(locks) == incomplete
    if incomplete:
        owner = json.loads((locks[0] / "owner.json").read_text())
        assert owner["terminal_recovery"]["status"] == "succeeded"
        assert owner["terminal_recovery"]["phase"] == (
            "terminal_views_published"
            if failure == "cleanup"
            else "observer_shutdown_complete"
        )


@pytest.mark.parametrize("cancellation", ["dashboard", "task"])
def test_interrupted_invocation_starts_full_count_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    original = cli.execute_workflow
    calls = 0
    stop_requested = False

    class StopHub(ObservabilityHub):
        @property
        def stop_requested(self) -> bool:
            return stop_requested

    async def interrupt_second_pass(*args: Any, **kwargs: Any) -> None:
        nonlocal calls, stop_requested
        calls += 1
        if calls == 2:
            (tmp_path / "retained.txt").write_text("partial edit", encoding="utf-8")
            if cancellation == "task":
                raise asyncio.CancelledError()
            stop_requested = True
            await asyncio.Event().wait()
        await original(*args, **kwargs)

    with (
        patch.object(cli, "execute_workflow", new=interrupt_second_pass),
        patch.object(cli, "ObservabilityHub", new=StopHub),
    ):
        if cancellation == "task":
            with pytest.raises(asyncio.CancelledError):
                project.run("--no-live")
        else:
            interrupted = project.run("--no-live")
            assert interrupted.exit_code == 130, interrupted.output
            assert "Run 3 of 3" not in interrupted.output
    records = project.manifests()
    assert calls == 2
    assert [record["status"] for record in records] == ["succeeded", "cancelled"]
    assert records[1]["cancel_reason"] == (
        "external_cancellation" if cancellation == "task" else "ui_stop_requested"
    )
    assert not list((tmp_path / ".crewplane/locks").iterdir())
    assert (tmp_path / "retained.txt").read_text() == "partial edit"
    result = project.run("--no-live")
    assert result.exit_code == 0, result.output
    assert "Run 1 of 3 (fresh)" in result.output
    assert len(project.manifests()) == 5
    assert all(
        record["status"] == "succeeded" and not record.get("resumed_nodes")
        for record in project.manifests()[2:]
    )


@pytest.mark.parametrize("count", [None, 3])
def test_keyboard_interruption_keeps_existing_cli_exit_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int | None
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, count, node_count=1)
    with patch.object(
        cli.workflow_runner, "execute_workflow_run", side_effect=KeyboardInterrupt
    ) as runner:
        result = project.run("--no-live", "--force")
    assert result.exit_code == 130
    assert runner.call_count == 1
    assert "Run 2" not in result.output


def test_tolerated_node_failures_do_not_stop_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    project.workflow["nodes"][0]["continue_on_failure"] = True
    project.workflow["nodes"][0]["mode"] = "parallel"
    project.write()
    with patch.object(
        MockAgentInvoker, "invoke", side_effect=provider_failure("tolerated failure")
    ):
        result = project.run("--no-live")
    assert result.exit_code == 0, result.output
    assert [record["status"] for record in project.manifests()] == ["succeeded"] * 3
    assert "failed" in result.output.lower()
