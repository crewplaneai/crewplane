import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import crewplane.cli.app as cli
from tests.integration.cli import repeat_force_run_support
from tests.integration.cli.repeat_force_run_support import (
    create_project,
    write_markdown_workflow,
)
from tests.integration.cli.workflow_runner_support import run_directories

run_allocation_clock = repeat_force_run_support.run_allocation_clock


@pytest.mark.usefixtures("run_allocation_clock")
@pytest.mark.parametrize("change", ["workflow", "import", "config", "static_input"])
def test_every_pass_reloads_execution_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    child = project.workflow_path.parent / "child.md"
    child_payload = {
        "name": "Child",
        "repeat_force_run_count": 4,
        "nodes": [{"id": "inspect", "mode": "sequential", "providers": ["alpha"]}],
    }
    if change == "import":
        project.workflow["imports"] = [{"path": "child.md", "as": "child"}]
        write_markdown_workflow(child, child_payload, {"inspect": "original"})
    if change == "static_input":
        project.prompts["node0"] = "Read {{file:input.txt}}"
        (tmp_path / "input.txt").write_text("original", encoding="utf-8")
    project.write()
    original = cli.workflow_runner.execute_workflow_run
    completed_runs: list[Path] = []

    async def run_and_edit(**kwargs: Any) -> None:
        previous_runs = set(run_directories(tmp_path))
        await original(**kwargs)
        new_runs = set(run_directories(tmp_path)) - previous_runs
        assert len(new_runs) == 1
        completed_runs.append(new_runs.pop())
        if len(completed_runs) != 1:
            return
        if change == "workflow":
            project.prompts["node0"] = "changed"
        elif change == "import":
            write_markdown_workflow(child, child_payload, {"inspect": "changed"})
        elif change == "config":
            project.config["agents"]["alpha"]["default_model"] = "changed-model"
        else:
            (tmp_path / "input.txt").write_text("changed", encoding="utf-8")
        project.write()

    with patch.object(cli.workflow_runner, "execute_workflow_run", new=run_and_edit):
        result = project.run("--no-live")
    assert result.exit_code == 0, (result.output, result.exception)
    signatures = [
        record["workflow_signature"] for record in project.manifests(completed_runs)
    ]
    assert len(signatures) == 3
    assert signatures[0] != signatures[1] == signatures[2]
    if change in {"workflow", "import", "static_input"}:
        node_id = "child.inspect" if change == "import" else "node0"
        contents = [
            (path / node_id / "alpha_executor_0_round1.md").read_text()
            for path in completed_runs
        ]
        assert "changed" not in contents[0]
        assert all("changed" in value for value in contents[1:])


@pytest.mark.parametrize(
    "change",
    [
        "invalid_count",
        "delete_workflow",
        "delete_config",
        "invalid_import",
        "missing_input",
        "enabled_workspace",
        "removed_count_enabled_workspace",
        "worktree",
        "imported_worktree",
    ],
)
def test_invalid_reload_stops_under_current_header_and_keeps_completed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.chdir(tmp_path)
    project = create_project(tmp_path, 3, node_count=1)
    if change == "missing_input":
        project.prompts["node0"] = "Read {{file:input.txt}}"
        (tmp_path / "input.txt").write_text("original", encoding="utf-8")
        project.write()
    original = cli.workflow_runner.execute_workflow_run
    calls = 0

    async def run_and_break(**kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        await original(**kwargs)
        (tmp_path / "retained.txt").write_text("retained edit", encoding="utf-8")
        if change == "invalid_count":
            project.workflow["repeat_force_run_count"] = "3"
        elif change == "delete_workflow":
            project.workflow_path.unlink()
            return
        elif change == "delete_config":
            project.config_path.unlink()
            return
        elif change == "invalid_import":
            project.workflow["imports"] = [{"path": "missing.md", "as": "child"}]
        elif change == "missing_input":
            (tmp_path / "input.txt").unlink()
        elif change in {"enabled_workspace", "removed_count_enabled_workspace"}:
            project.config["settings"]["workspace"]["enabled"] = True
            if change.startswith("removed"):
                project.workflow.pop("repeat_force_run_count")
        elif change == "worktree":
            project.workflow["worktrees"] = {"unused": {"kind": "snapshot"}}
            project.workflow["nodes"][0]["worktree"] = "none"
        else:
            child = project.workflow_path.parent / "child.md"
            write_markdown_workflow(
                child,
                {
                    "name": "Child",
                    "worktrees": {"unused": {"kind": "snapshot"}},
                    "nodes": [],
                },
                {},
            )
            project.workflow["imports"] = [{"path": "child.md", "as": "child"}]
        project.write()

    with patch.object(cli.workflow_runner, "execute_workflow_run", new=run_and_break):
        result = project.run("--no-live")
    assert result.exit_code == 1, result.output
    assert "Run 2 of 3 (fresh)" in result.output
    assert "Run 3 of 3" not in result.output
    assert "Invalid:" not in result.output.split("Run 2 of 3 (fresh)")[0]
    assert calls == (2 if change == "missing_input" else 1)
    manifests = project.manifests()
    assert [record["status"] for record in manifests] == ["succeeded"]
    directories = run_directories(tmp_path)
    assert len(directories) == 2
    preflight_statuses = [
        json.loads((path / "preflight/manifest.json").read_text())["status"]
        for path in directories
    ]
    assert sorted(preflight_statuses) == ["preflight_failed", "preflight_succeeded"]
    assert (tmp_path / "retained.txt").read_text() == "retained edit"
    assert (
        len(
            list(
                (tmp_path / ".crewplane/execution-stages").glob(
                    "*/preflight/diagnostics.json"
                )
            )
        )
        == 1
    )
    assert not list((tmp_path / ".crewplane/locks").iterdir())
