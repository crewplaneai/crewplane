from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import crewplane.cli.app as cli
from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.architecture.contracts import InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.observability import ObservabilityHub
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.integration.cli import repeat_force_run_support
from tests.integration.cli.repeat_force_run_support import create_project
from tests.integration.cli.workflow_runner_support import (
    result_directories,
    run_directories,
)

isolated_git = isolated_git_support.isolated_git
run_allocation_clock = repeat_force_run_support.run_allocation_clock


def test_rapid_repetition_uses_existing_unique_run_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    base = datetime(2026, 9, 22, 12)
    with patch("crewplane.artifacts.directory_manager.datetime") as allocation_clock:
        allocation_clock.now.side_effect = [
            base + timedelta(microseconds=index) for index in range(5)
        ]
        result = project.run("--no-live")
    assert result.exit_code == 0, result.output
    assert [manifest["run_id"] for manifest in project.manifests()] == [
        "20260922-120000",
        "20260922-120000-000002",
        "20260922-120000-000004",
    ]
    assert len(run_directories(tmp_path)) == len(result_directories(tmp_path)) == 3


@pytest.mark.parametrize("count", [None, 1, 3])
@pytest.mark.parametrize("force", [False, True])
def test_force_count_matrix_bypasses_successful_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int | None, force: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, None, node_count=1)
    first = project.run("--no-live")
    assert first.exit_code == 0, first.output
    project.set_count(count)
    result = project.run("--no-live", *(["--force"] if force else []))
    assert result.exit_code == 0, result.output
    expected = (count or 1) if count is not None or force else 0
    manifests = project.manifests()
    assert len(manifests) == 1 + expected
    assert len({manifest["workflow_signature"] for manifest in manifests}) == 1
    assert all(not manifest.get("resumed_nodes") for manifest in manifests)
    if count is not None:
        assert [
            f"Run {index} of {count} (fresh)" in result.output
            for index in range(1, count + 1)
        ] == [True] * count
    else:
        assert "(fresh)" not in result.output


@pytest.mark.usefixtures("run_allocation_clock")
def test_three_passes_match_manual_forced_runs_and_retain_project_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_git: IsolatedGit
) -> None:
    observed_runs: dict[Path, list[str]] = {}
    observed_nodes: dict[Path, list[str]] = {}
    observed_reads: dict[Path, list[tuple[str, str]]] = {}
    lifecycle: dict[Path, list[str]] = {}
    event_loops: dict[Path, list[asyncio.AbstractEventLoop]] = {}
    original_invoke = MockAgentInvoker.invoke

    class RecordingHub(ObservabilityHub):
        def __enter__(self):  # type: ignore[no-untyped-def]
            root = Path.cwd()
            previous = [
                root / ".crewplane/execution-stages" / key
                for key in observed_runs[root]
            ]
            assert all(
                json.loads((path / "manifests/run.json").read_text())["status"]
                == "succeeded"
                for path in previous
            )
            assert len(lifecycle[root]) % 2 == 0
            lifecycle[root].append("start")
            return super().__enter__()

        def __exit__(self, *args: Any) -> None:
            super().__exit__(*args)
            lifecycle[Path.cwd()].append("stop")

    async def record_invocation(
        self: MockAgentInvoker,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        context = invocation_context
        assert context is not None
        assert cwd == Path.cwd()
        observed_nodes[cwd].append(context.node_id)
        if context.node_id == "node0":
            run_key = next(
                path.name
                for path in run_directories(cwd)
                if json.loads((path / "manifests/run.json").read_text())["status"]
                == "running"
            )
            observed_runs[cwd].append(run_key)
            event_loops[cwd].append(asyncio.get_running_loop())
            if len(observed_runs[cwd]) == 1:
                (cwd / "source.txt").write_text("edited", encoding="utf-8")
                (cwd / "created.txt").write_text("untracked", encoding="utf-8")
            observed_reads[cwd].append(
                ((cwd / "source.txt").read_text(), (cwd / "created.txt").read_text())
            )
        else:
            current_key = observed_runs[cwd][-1]
            assert current_key in prompt
            assert all(f"/{key}/" not in prompt for key in observed_runs[cwd][:-1]), (
                observed_runs[cwd],
                prompt,
            )
        await original_invoke(
            self, config, model, prompt, output_file, cwd, log_file, context
        )

    monkeypatch.setattr(MockAgentInvoker, "invoke", record_invocation)
    monkeypatch.setattr(cli, "ObservabilityHub", RecordingHub)
    results = []
    for name, count in [("repeated", 3), ("manual", None)]:
        root = tmp_path / name
        project = create_project(root, count)
        monkeypatch.chdir(root)
        (root / "source.txt").write_text("original", encoding="utf-8")
        isolated_git.run(root, "init")
        isolated_git.run(root, "add", "source.txt")
        isolated_git.run(root, "commit", "-m", "initial source")
        observed_runs[root], observed_nodes[root], observed_reads[root] = [], [], []
        lifecycle[root], event_loops[root] = [], []
        for invocation_index in range(1 if count else 3):
            result = project.run("--no-live", "--force")
            assert result.exit_code == 0, (result.output, result.exception)
            assert len(observed_runs[root]) == (count or invocation_index + 1)
        manifests = project.manifests()
        assert len(manifests) == len(result_directories(root)) == 3
        assert len({record["run_id"] for record in manifests}) == 3
        assert len({record["workflow_signature"] for record in manifests}) == 1
        assert all(
            record["status"] == "succeeded" and not record.get("resumed_nodes")
            for record in manifests
        )
        assert not list((root / ".crewplane/locks").iterdir())
        assert not list(
            (root / ".crewplane/execution-stages").rglob("resume-source.json")
        )
        assert observed_nodes[root] == [f"node{i}" for i in range(5)] * 3
        assert observed_reads[root] == [("edited", "untracked")] * 3
        status = isolated_git.run_text(
            root, "status", "--porcelain", "--", "source.txt", "created.txt"
        )
        assert "M source.txt" in status
        assert "?? created.txt" in status
        assert lifecycle[root] == ["start", "stop"] * 3
        if count:
            assert len(set(event_loops[root])) == 1
        normalized = []
        results_by_name = {path.name: path for path in result_directories(root)}
        for run_key in observed_runs[root]:
            directory = results_by_name[run_key]
            normalized.append(
                {
                    path.name: path.read_text()
                    .replace(str(root), "ROOT")
                    .replace(directory.name, "RUN")
                    for path in sorted(directory.glob("*.md"))
                }
            )
        results.append(normalized)
    # Result wrappers include timestamps; compare the deterministic provider bodies.
    assert [
        [text.split("## Output\n", 1)[-1] for text in run.values()]
        for run in results[0]
    ] == [
        [text.split("## Output\n", 1)[-1] for text in run.values()]
        for run in results[1]
    ]


@pytest.mark.usefixtures("run_allocation_clock")
@pytest.mark.parametrize("later_count", [1, 9, None])
def test_reload_count_changes_keep_original_total_and_manifest_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later_count: int | None
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    original = cli.workflow_runner.execute_workflow_run
    captured_forces = []
    completed_runs: list[Path] = []

    async def run_and_edit(**kwargs: Any) -> None:
        captured_forces.append(kwargs["force"])
        previous_runs = set(run_directories(tmp_path))
        await original(**kwargs)
        new_runs = set(run_directories(tmp_path)) - previous_runs
        assert len(new_runs) == 1
        completed_runs.append(new_runs.pop())
        project.set_count(later_count)
        # Later loads must use the selected path even when discovery becomes ambiguous.
        (project.workflow_path.parent / "extra.task.md").write_text(
            "invalid", encoding="utf-8"
        )

    with patch.object(cli.workflow_runner, "execute_workflow_run", new=run_and_edit):
        from typer.testing import CliRunner

        result = CliRunner().invoke(cli.app, ["run", "--no-live"])
    assert result.exit_code == 0, result.output
    assert captured_forces == [True, True, True]
    manifests = project.manifests(completed_runs)
    assert [
        record["composed_workflow"].get("repeat_force_run_count")
        for record in manifests
    ] == [3, later_count, later_count]
    for record, count in zip(manifests, [3, later_count, later_count], strict=True):
        assert ("repeat_force_run_count" in record["workflow_source"]) == (
            count is not None
        )
        assert ("repeat_force_run_count" in record["composed_workflow"]) == (
            count is not None
        )
    assert len({record["workflow_signature"] for record in manifests}) == 1
    project.set_count(None)
    result = project.run("--no-live")
    assert result.exit_code == 0, result.output
    assert "Identical context detected" in result.output
    assert len(project.manifests()) == 3
