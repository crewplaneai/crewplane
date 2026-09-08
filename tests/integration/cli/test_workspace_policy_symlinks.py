from __future__ import annotations

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


@pytest.mark.parametrize("policy_name", [".gitignore", ".gitattributes"])
@pytest.mark.parametrize("policy_directory", [".", "nested"])
def test_policy_symlinks_are_rejected_before_provider_or_workspace_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    policy_name: str,
    policy_directory: str,
) -> None:
    project = tmp_path / "project"
    policy_parent = project / policy_directory
    policy_parent.mkdir(parents=True)
    (policy_parent / "rules").write_text("# policy\n", encoding="utf-8")
    (policy_parent / policy_name).symlink_to("rules")
    workflow_path = project / "workflow.task.md"
    workflow_path.write_text(
        f"""---
schema_version: '{SCHEMA_VERSION}'
name: PolicySymlink
worktrees:
  implementation:
    kind: worktree
nodes:
  - id: implement
    mode: sequential
    providers: [alpha]
---
## implement
Inspect the project.
""",
        encoding="utf-8",
    )
    isolated_git.run_text(project, "init")
    isolated_git.run_text(project, "add", ".")
    isolated_git.run_text(project, "commit", "-m", "record policy symlink")
    provider_marker = tmp_path / "provider-called"
    cache_root = tmp_path / "cache"
    config = Config(
        version=SCHEMA_VERSION,
        agents={
            "alpha": AgentConfig(
                cli_cmd=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    f"Path({provider_marker.as_posix()!r}).touch(); print('done')",
                ],
                provider_kind="generic",
                max_retries=0,
            )
        },
        settings=Settings(
            workspace={"enabled": True, "cache_root": cache_root.as_posix()},
            integrations={"ui": {"implementation": "none"}},
        ),
    )
    state_dir = project / ".crewplane"
    state_dir.mkdir()
    (state_dir / "config.yml").write_text(config.model_dump_json(), encoding="utf-8")
    isolated_git.run_text(project, "add", ".crewplane/config.yml")
    isolated_git.run_text(project, "commit", "-m", "configure workspace")
    monkeypatch.chdir(project)
    runner = CliRunner()

    for options in (["--dry-run"], []):
        result = runner.invoke(
            app, ["run", "--tasks", "workflow.task.md", "--no-live", *options]
        )
        assert result.exit_code == 1, result.output
        assert "symlinked Git policy files are unsupported" in " ".join(
            result.output.split()
        )
        assert policy_name in result.output
        assert not provider_marker.exists()
        assert not cache_root.exists()
        assert not list(state_dir.glob("execution-stages/**/workspace-state*.json"))
