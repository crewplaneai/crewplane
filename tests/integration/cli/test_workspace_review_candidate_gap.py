from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.cli.app import app
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_review import review_workflow, write_review_fixtures
from tests.helpers.workspace_workflow_fixtures import (
    run_dirs,
    workspace_states,
    write_fixture,
)
from tests.helpers.workspace_workflow_runner import (
    run_workspace_workflow,
    workspace_config,
    workspace_project,
)

pytestmark = requires_workspace_support


isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("resume_after_failure", [False, True])
def test_discarded_review_round_preserves_skip_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    resume_after_failure: bool,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = review_workflow()
    write_review_fixtures(fixtures)
    monkeypatch.chdir(project)

    if resume_after_failure:
        with pytest.raises(RuntimeError, match="mock invoker failed"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
        failed_run = run_dirs(project)[0]
        _assert_review_gap(failed_run)
        shutil.rmtree(fixtures / "implement")

    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed candidate.\n")
    stream = io.StringIO()
    asyncio.run(run_workspace_workflow(workflow, config, Console(file=stream)))
    successful_runs = run_dirs(project)
    manifest = json.loads(
        (successful_runs[-1] / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "succeeded"
    if resume_after_failure:
        assert "Resuming workflow" in stream.getvalue()
        assert manifest["resumed_nodes"] == ["implement"]
    else:
        _assert_review_gap(successful_runs[-1])
        shutil.rmtree(fixtures / "implement")

    duplicate_stream = io.StringIO()
    asyncio.run(
        run_workspace_workflow(workflow, config, Console(file=duplicate_stream))
    )

    assert run_dirs(project) == successful_runs
    assert "Identical context detected" in duplicate_stream.getvalue()


def test_cleanup_removes_workspaces_after_discarded_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    write_review_fixtures(fixtures)
    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed candidate.\n")
    monkeypatch.chdir(project)
    asyncio.run(
        run_workspace_workflow(review_workflow(), config, Console(file=io.StringIO()))
    )
    run_dir = run_dirs(project)[0]
    _assert_review_gap(run_dir)
    state_paths = sorted(run_dir.glob("*/workspace-state*.json"))
    states = [json.loads(path.read_text(encoding="utf-8")) for path in state_paths]
    workspace_paths = [Path(state["execution"]["workspace_path"]) for state in states]
    assert len(workspace_paths) == 6
    assert all(path.is_dir() for path in workspace_paths)
    config_path = project / ".crewplane" / "cleanup-config.yml"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert all(not path.exists() for path in workspace_paths), result.output
    for state_path, original in zip(state_paths, states, strict=True):
        cleaned = json.loads(state_path.read_text(encoding="utf-8"))
        assert cleaned["status"] == original["status"] == "succeeded"
        assert cleaned["workspace"]["retention"] == "deleted"


def _assert_review_gap(run_dir: Path) -> None:
    states = workspace_states(run_dir / "implement")
    executors = {
        state["round_num"]: state for state in states if state["role"] == "executor"
    }
    assert set(executors) == {1, 2, 3}
    assert executors[2]["result"]["lineage_discarded"] is True
    assert executors[2]["workspace"]["lineage_producer"] is False
    assert executors[3]["source"]["commit"] == executors[1]["result"]["result_commit"]
    assert sorted(
        state["round_num"] for state in states if state["role"] == "reviewer"
    ) == [
        1,
        3,
    ]
