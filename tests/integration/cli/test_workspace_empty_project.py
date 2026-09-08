from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.version import SCHEMA_VERSION
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit

isolated_git = isolated_git_support.isolated_git


def test_worktree_cli_launches_in_untracked_project_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Repository\n", encoding="utf-8")
    isolated_git.run_text(repo, "init")
    isolated_git.run_text(repo, "add", "README.md")
    isolated_git.run_text(repo, "commit", "-m", "initial")
    project_root = repo / "new-app"
    workflow_root = project_root / ".crewplane" / "workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "create.task.md").write_text(
        f"""---
schema_version: '{SCHEMA_VERSION}'
name: EmptyProject
worktrees:
  implementation:
    kind: worktree
nodes:
  - id: implement
    mode: sequential
    providers: [alpha]
---
## implement
Create the application.
""",
        encoding="utf-8",
    )
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(
                cli_cmd=[
                    sys.executable,
                    "-c",
                    "import sys; sys.stdin.read(); "
                    "from pathlib import Path; "
                    "Path('created.txt').write_text('created\\n'); "
                    "print('Created created.txt.')",
                ],
                provider_kind="generic",
                max_retries=0,
            )
        },
        settings=Settings(
            workspace={
                "enabled": True,
                "cache_root": (tmp_path / "cache").as_posix(),
                "clean_start": "tracked_only",
            },
            integrations={"ui": {"implementation": "none"}},
        ),
    )
    (project_root / ".crewplane" / "config.yml").write_text(
        config.model_dump_json(), encoding="utf-8"
    )
    monkeypatch.chdir(project_root)
    runner = CliRunner()

    dry_run = runner.invoke(app, ["run", "--dry-run", "--no-live"])
    assert dry_run.exit_code == 0, dry_run.output
    stages_root = project_root / ".crewplane" / "execution-stages"
    assert not stages_root.exists()

    result = runner.invoke(app, ["run", "--no-live"])
    assert result.exit_code == 0, result.output
    (state_path,) = stages_root.glob("*/implement/workspace-state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "succeeded"
    result_commit = state["result"]["result_commit"]
    assert (
        isolated_git.run_text(repo, "show", f"{result_commit}:new-app/created.txt")
        == "created"
    )
    assert not (project_root / "created.txt").exists()
