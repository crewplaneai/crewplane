from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.helpers import isolated_git as _isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.workspace_workflow_runner import (
    run_workspace_enabled_mock_e2e,
    run_workspace_real_run_rejects_non_filesystem_artifacts,
)

pytestmark = requires_workspace_support


isolated_git = _isolated_git_support.isolated_git


def test_workspace_enabled_mock_e2e_public_run_surfaces(
    tmp_path: Path,
    isolated_git: IsolatedGit,
) -> None:
    asyncio.run(run_workspace_enabled_mock_e2e(tmp_path, isolated_git))


def test_workspace_enabled_real_run_rejects_non_filesystem_artifacts(
    tmp_path: Path,
    isolated_git: IsolatedGit,
) -> None:
    asyncio.run(
        run_workspace_real_run_rejects_non_filesystem_artifacts(
            tmp_path,
            isolated_git,
        )
    )


def test_descendant_managed_run_keeps_nested_project_and_source_fixtures(
    tmp_path: Path,
    isolated_git: IsolatedGit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import yaml
    from typer.testing import CliRunner

    import crewplane.cli.app as cli
    from tests.helpers.foreach import workflow_document, write_workflow_document
    from tests.helpers.mock_resume import write_executor_fixture
    from tests.helpers.workspace_workflow_runner import workspace_config
    from tests.integration.cli.cli_workflow_helpers import (
        mock_invocations,
        write_basic_workflow,
    )
    from tests.integration.cli.workflow_runner_support import run_directories

    repository = tmp_path / "repository"
    project = repository / "apps/service"
    child = project / "backend/api"
    child.mkdir(parents=True)
    workflow = project / ".crewplane/workflows/workflow.task.md"
    workflow.parent.mkdir(parents=True)
    write_basic_workflow(workflow)
    payload, body = workflow_document(workflow)
    payload["worktrees"] = {"implementation": {"kind": "worktree"}}
    payload["nodes"][0]["worktree"] = "implementation"
    write_workflow_document(workflow, payload, body)
    (project / "app.txt").write_bytes(b"source application\n")
    (repository / ".gitignore").write_bytes(b".crewplane/\nfixtures/\n")
    isolated_git.run(repository, "init")
    isolated_git.run(repository, "add", ".")
    isolated_git.run(repository, "commit", "-m", "source project")
    fixtures = project / "fixtures"
    write_executor_fixture(fixtures, "review.node", "Source fixture result\n")
    (fixtures / "review.node/alpha_executor_0_round1.mutations.json").write_text(
        json.dumps(
            {
                "workspace_mutations": [
                    {"path": "app.txt", "content": "managed change\n"}
                ]
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    config = workspace_config(tmp_path / "cache", Path("fixtures"))
    (project / ".crewplane/config.yml").write_text(
        yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.chdir(child)
    result = CliRunner().invoke(cli.app, ["run", "--no-live"])
    assert result.exit_code == 0, (result.output, result.exception)
    (record,) = mock_invocations(project)
    managed_cwd = Path(record["cwd"])
    assert managed_cwd.parts[-2:] == ("apps", "service")
    assert managed_cwd != project
    assert (managed_cwd / "app.txt").read_bytes() == b"managed change\n"
    assert (project / "app.txt").read_bytes() == b"source application\n"
    assert not (managed_cwd / "fixtures").exists()
    assert Path(record["fixture_path"]).is_relative_to(fixtures)
    (run,) = run_directories(project)
    plan = json.loads((run / "preflight/execution-plan.json").read_bytes())
    assert plan["workspace_source"]["project_root_relative_path"] == "apps/service"
    assert not (child / ".crewplane").exists()
    assert Path.cwd() == child
