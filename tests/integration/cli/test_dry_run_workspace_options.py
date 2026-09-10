from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from crewplane.cli.app import app
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.version import SCHEMA_VERSION
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.integration.cli.dry_run_helpers import artifact_tree

isolated_git = isolated_git_support.isolated_git


def write_preview_project(
    root: Path, declaration_options: str = "", include_input: bool = False
) -> None:
    workflow_root = root / ".crewplane" / "workflows"
    workflow_root.mkdir(parents=True)
    (root / "requirements.md").write_text("Build the feature.\n", encoding="utf-8")
    config = Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["__provider_must_not_run__"])},
        settings=Settings.model_validate(
            {
                "workspace": {
                    "enabled": True,
                    "cache_root": str(root.parent / "workspace-cache"),
                    "setup_profiles": {
                        "prepare": {
                            "run": [
                                ["__setup_must_not_run__", "first"],
                                ["__setup_must_not_run__", "second"],
                            ]
                        }
                    },
                },
                "integrations": {
                    "invoker": {"implementation": "mock"},
                    "ui": {"implementation": "none"},
                },
            }
        ),
    )
    (root / ".crewplane" / "config.yml").write_text(
        config.model_dump_json(), encoding="utf-8"
    )
    input_frontmatter = (
        "  - id: requirements\n"
        "    mode: input\n"
        "    source: '{{file:requirements.md}}'\n"
        if include_input
        else ""
    )
    dependency = "    needs: [requirements]\n" if include_input else ""
    prompt = "Use {{requirements.output}}." if include_input else "Build the feature."
    (workflow_root / "preview.task.md").write_text(
        f"---\nschema_version: '{SCHEMA_VERSION}'\nname: Workspace Preview\n"
        f"worktrees:\n  primary:\n    kind: worktree\n{declaration_options}"
        f"nodes:\n{input_frontmatter}  - id: implement\n    mode: sequential\n"
        f"{dependency}    providers: [alpha]\n---\n"
        f"## implement\n\n{prompt}\n",
        encoding="utf-8",
    )


def run_preview_without_side_effects(
    root: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> str:
    isolated_git.run_text(root, "init")
    isolated_git.run_text(root, "add", ".")
    isolated_git.run_text(root, "commit", "-m", "preview input")
    initial_refs = isolated_git.run_text(root, "show-ref")
    initial_worktrees = isolated_git.run_text(root, "worktree", "list", "--porcelain")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["run", "--dry-run", "--no-live"])

    assert result.exit_code == 0, result.output
    assert "Dry run mode" in result.output
    assert "Workspace: enabled" in result.output
    assert artifact_tree(root / ".crewplane") == ()
    assert not (root.parent / "workspace-cache").exists()
    assert isolated_git.run_text(root, "status", "--porcelain") == ""
    assert isolated_git.run_text(root, "show-ref") == initial_refs
    assert (
        isolated_git.run_text(root, "worktree", "list", "--porcelain")
        == initial_worktrees
    )
    return " ".join(result.output.split())


def test_dry_run_displays_workspace_setup_without_running_commands(
    tmp_path: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    write_preview_project(root, "    setup_profile: prepare\n")

    output = run_preview_without_side_effects(root, isolated_git, monkeypatch)

    assert "setup: profile=prepare commands=2" in output
    assert "result=deterministic_commit_tree" in output


@pytest.mark.parametrize(
    ("branch_option", "branch_label"),
    [
        pytest.param(
            "    branch_name: feature/preview\n", "feature/preview", id="explicit"
        ),
        pytest.param("", "(generated)", id="generated"),
    ],
)
def test_dry_run_displays_branch_export_without_creating_refs(
    tmp_path: Path,
    isolated_git: IsolatedGit,
    monkeypatch: pytest.MonkeyPatch,
    branch_option: str,
    branch_label: str,
) -> None:
    root = tmp_path / "project"
    write_preview_project(root, "    create_branch: true\n" + branch_option)

    output = run_preview_without_side_effects(root, isolated_git, monkeypatch)

    assert f"branch export: name={branch_label}" in output


def test_dry_run_keeps_input_files_outside_managed_workspaces(
    tmp_path: Path, isolated_git: IsolatedGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    write_preview_project(root, include_input=True)

    output = run_preview_without_side_effects(root, isolated_git, monkeypatch)

    assert "Node: requirements (input)" in output
    input_summary = output.split("Node: requirements (input)", 1)[1].split(
        "Node: implement", 1
    )[0]
    assert "source: {{file:requirements.md}}" in input_summary
    assert "workspace:" not in input_summary
    assert "Node: implement (sequential) needs: requirements" in output
    assert "workspace: worktree name=primary" in output
