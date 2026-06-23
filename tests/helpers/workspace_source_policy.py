from __future__ import annotations

import subprocess
from pathlib import Path

from crewplane.cli.run.workspace import source_policy as policy
from crewplane.cli.run.workspace.git_source import GitSourceContext
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION


def workspace_source_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["echo"])},
        settings=Settings(workspace={"enabled": True}),
    )


def workspace_source_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace source",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="run")],
            )
        ],
    )


def workspace_input_only_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace input",
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/input.md}}",
            )
        ],
    )


def git_source_context(tmp_path: Path) -> GitSourceContext:
    git_dir = tmp_path / ".git"
    return GitSourceContext(
        run_base_commit="a" * 40,
        source_tree="b" * 40,
        object_format="sha1",
        git_top_level=tmp_path,
        project_root_relative_path=".",
        active_git_dir=git_dir,
        common_git_dir=git_dir,
        git_version="git version 2.34.1",
    )


def run_git_text(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()


def apply_patched_git_policy(monkeypatch, tmp_path: Path) -> None:
    def fake_discover_git_context(
        project_root: Path,
        builder: policy.WorkspacePolicyBuilder,
    ) -> GitSourceContext:
        del project_root, builder
        return git_source_context(tmp_path)

    def noop(*args: object, **kwargs: object) -> None:
        del args, kwargs

    monkeypatch.setattr(
        policy,
        "discover_git_context",
        fake_discover_git_context,
    )
    for name in (
        "validate_unsupported_repo_state",
        "validate_git_capabilities",
        "validate_local_git_config",
        "validate_local_policy_files",
        "validate_index_extensions",
        "validate_index_flags",
        "validate_clean_start",
        "validate_source_tree",
        "warn_storage_pressure",
    ):
        monkeypatch.setattr(policy, name, noop)
