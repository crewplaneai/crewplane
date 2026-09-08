from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.cli.workflow_runner import execute_workflow_run
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight.source import load_workflow_source_for_preflight
from crewplane.core.workspace.policy import WorktreeKind
from crewplane.version import SCHEMA_VERSION
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("kind", ["worktree", "snapshot"])
def test_workspace_nodes_with_normalized_names_keep_distinct_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    kind: WorktreeKind,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    workflow_path = project_root / "workflow.task.md"
    workflow_path.write_text(
        f"""---
schema_version: '{SCHEMA_VERSION}'
name: WorkspaceIdentity
worktrees:
  implementation:
    kind: {kind}
nodes:
  - id: a
    mode: sequential
    providers: [alpha]
  - id: .a
    mode: sequential
    needs: [a]
    providers: [alpha]
---
## a
Inspect the workflow source.
## .a
Inspect the workflow source.
""",
        encoding="utf-8",
    )
    isolated_git.run_text(project_root, "init")
    isolated_git.run_text(project_root, "add", "workflow.task.md")
    isolated_git.run_text(project_root, "commit", "-m", "initial")
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="mock")},
        settings=Settings(
            workspace={
                "enabled": True,
                "cache_root": (tmp_path / "cache").as_posix(),
                "cleanup_on_success": kind == "worktree",
            },
            integrations={
                "invoker": {
                    "implementation": "mock",
                    "options": {"observation_delay_seconds": 0},
                },
                "ui": {"implementation": "none"},
            },
        ),
    )
    monkeypatch.chdir(project_root)
    source = load_workflow_source_for_preflight(workflow_path, project_root)
    stream = io.StringIO()
    console = Console(file=stream)

    asyncio.run(
        execute_workflow_run(config, source, force=False, no_live=True, console=console)
    )

    stages_root = project_root / ".crewplane" / "execution-stages"
    run_dirs = tuple(stages_root.iterdir())
    assert len(run_dirs) == 1
    states = [
        json.loads(
            (run_dirs[0] / node_id / "workspace-state.json").read_text(encoding="utf-8")
        )
        for node_id in ("a", ".a")
    ]
    assert [state["status"] for state in states] == ["succeeded", "succeeded"]
    if kind == "worktree":
        assert states[0]["refs"]["result"] != states[1]["refs"]["result"]
        assert states[1]["source"]["commit"] == states[0]["result"]["result_commit"]
    else:
        paths = [Path(state["execution"]["workspace_path"]) for state in states]
        assert paths[0] != paths[1]
        assert all(path.is_dir() for path in paths)

    asyncio.run(
        execute_workflow_run(config, source, force=False, no_live=True, console=console)
    )

    assert tuple(stages_root.iterdir()) == run_dirs
    assert "Identical context detected" in stream.getvalue()


@pytest.mark.parametrize("node_id", ["implement-", "a" * 121])
def test_cleanup_removes_reviewer_workspaces_with_normalized_node_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    node_id: str,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    workflow_path = project_root / "workflow.task.md"
    workflow_path.write_text(
        f"""---
schema_version: '{SCHEMA_VERSION}'
name: WorkspaceReviewerIdentity
worktrees:
  implementation:
    kind: worktree
nodes:
  - id: {node_id}
    mode: sequential
    providers:
      - provider: alpha
        role: executor
      - provider: alpha
        role: reviewer
---
## {node_id}
Inspect the workflow source.
""",
        encoding="utf-8",
    )
    isolated_git.run_text(project_root, "init")
    isolated_git.run_text(project_root, "add", "workflow.task.md")
    isolated_git.run_text(project_root, "commit", "-m", "initial")
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="mock")},
        settings=Settings(
            workspace={
                "enabled": True,
                "cache_root": (tmp_path / "cache").as_posix(),
                "cleanup_on_success": False,
            },
            integrations={
                "invoker": {
                    "implementation": "mock",
                    "options": {"observation_delay_seconds": 0},
                },
                "ui": {"implementation": "none"},
            },
        ),
    )
    monkeypatch.chdir(project_root)
    source = load_workflow_source_for_preflight(workflow_path, project_root)
    asyncio.run(
        execute_workflow_run(
            config,
            source,
            force=False,
            no_live=True,
            console=Console(file=io.StringIO()),
        )
    )
    stages_root = project_root / ".crewplane" / "execution-stages"
    states = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in stages_root.glob(f"*/{node_id}/workspace-state*.json")
    ]
    assert {state["role"] for state in states} == {"executor", "reviewer"}
    workspace_paths = [Path(state["execution"]["workspace_path"]) for state in states]
    assert all(path.is_dir() for path in workspace_paths)
    config_path = project_root / ".crewplane" / "cleanup-config.yml"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert all(not path.exists() for path in workspace_paths), result.output
