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
from crewplane.core.config import Config
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_workflow_fixtures import (
    review_output,
    run_dirs,
    workspace_states,
    write_fixture,
)
from tests.helpers.workspace_workflow_runner import (
    run_workspace_workflow,
    workspace_config,
    workspace_project,
)

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("kind", ["worktree", "snapshot"])
@pytest.mark.parametrize("resume_after_failure", [False, True])
def test_tolerated_reviewer_failure_preserves_skip_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    kind: str,
    resume_after_failure: bool,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = _review_workflow(kind)
    write_fixture(fixtures, "implement", "executor-round-1.md", "Implemented.\n")
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-1.md",
        review_output("NO_FINDINGS", "None"),
    )
    monkeypatch.chdir(project)

    if resume_after_failure:
        with pytest.raises(RuntimeError, match="mock invoker failed"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
        _assert_tolerated_reviewer_failure(run_dirs(project)[0])
        shutil.rmtree(fixtures / "implement")

    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed.\n")
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
        _assert_tolerated_reviewer_failure(successful_runs[-1])
        shutil.rmtree(fixtures / "implement")

    duplicate_stream = io.StringIO()
    asyncio.run(
        run_workspace_workflow(workflow, config, Console(file=duplicate_stream))
    )

    assert run_dirs(project) == successful_runs
    assert "Identical context detected" in duplicate_stream.getvalue()
    if resume_after_failure:
        _assert_workspace_cleanup(project, config)


def _assert_workspace_cleanup(project: Path, config: Config) -> None:
    workspace_paths = [
        Path(path)
        for run_dir in run_dirs(project)
        for node_id in ("implement", "consume")
        for state in workspace_states(run_dir / node_id)
        if isinstance(path := state["execution"]["workspace_path"], str)
    ]
    assert any(path.exists() for path in workspace_paths)
    config_path = project / ".crewplane" / "cleanup-config.yml"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", config_path.as_posix(), "--yes"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert all(not path.exists() for path in workspace_paths), result.output


def _assert_tolerated_reviewer_failure(run_dir: Path) -> None:
    states = workspace_states(run_dir / "implement")
    assert {
        (state["role"], state["round_num"], state["status"]) for state in states
    } == {
        ("reviewer", 0, "failed"),
        ("executor", 1, "succeeded"),
        ("reviewer", 1, "succeeded"),
    }


def _review_workflow(kind: str) -> WorkflowPlan:
    return WorkflowPlan(
        name="ToleratedReviewerFailure",
        worktrees={"code": {"kind": kind}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                depth=1,
                review_starts_with="reviewer",
                continue_on_failure=True,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="alpha", role="reviewer"),
                ],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Implement and review {{file:src/app.txt}}.",
                    )
                ],
            ),
            WorkflowNode(
                id="consume",
                mode="sequential",
                needs=["implement"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="Consume.")],
            ),
        ],
    )
