from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.preflight.models import WorkspaceSourceSnapshot
from crewplane.core.prompt_segments import PromptSegment
from crewplane.core.workflow.models import (
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.version import SCHEMA_VERSION


def workspace_config(workspace: dict[str, object] | None = None) -> Config:
    workspace_settings = workspace if workspace is not None else {"enabled": True}
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["echo"])},
        settings=Settings(workspace=workspace_settings),
    )


def workspace_source_snapshot(
    commit: str,
    git_version: str = "git version 2.34.1",
    git_top_level: str = "/repo",
    active_git_dir: str = "/repo/.git",
    common_git_dir: str = "/repo/.git",
) -> WorkspaceSourceSnapshot:
    return WorkspaceSourceSnapshot(
        worktree_contract=WorktreeContract(),
        run_base_commit=commit,
        source_tree="b" * 40,
        object_format="sha1",
        repository_id="c" * 64,
        git_version=git_version,
        git_top_level=git_top_level,
        project_root_relative_path=".",
        active_git_dir=active_git_dir,
        common_git_dir=common_git_dir,
        clean_start="strict",
    )


def init_git_repo(root: Path) -> WorkspaceSourceSnapshot:
    git(root, "init")
    git(root, "config", "user.name", "Crewplane Test")
    git(root, "config", "user.email", "crewplane-test@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return workspace_source_snapshot(
        git(root, "rev-parse", "HEAD"),
        git_top_level=root.as_posix(),
        active_git_dir=(root / ".git").as_posix(),
        common_git_dir=(root / ".git").as_posix(),
    ).model_copy(update={"source_tree": git(root, "rev-parse", "HEAD^{tree}")})


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()


def workspace_workflow(prompt: str = "run") -> WorkflowPlan:
    return WorkflowPlan(
        name="workspace preflight",
        worktrees={"primary": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content=prompt)],
            )
        ],
    )


def compile_workspace_preview(root: Path, commit: str):
    return compile_with_source_snapshot(root, workspace_source_snapshot(commit))


def compile_with_source_snapshot(
    root: Path,
    source_snapshot: WorkspaceSourceSnapshot,
):
    return compile_workflow_with_source_snapshot(
        root,
        workspace_workflow(),
        source_snapshot,
    )


def compile_workflow_with_source_snapshot(
    root: Path,
    workflow: WorkflowPlan,
    source_snapshot: WorkspaceSourceSnapshot | None,
    allowed_template_paths: tuple[Path, ...] = (),
):
    config = workspace_config()
    runtime_snapshot = build_runtime_config_snapshot(
        config=config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=runtime_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            allowed_template_paths=allowed_template_paths,
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )
