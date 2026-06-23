from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from crewplane.architecture.contracts import CanonicalIntegrationConfig
from crewplane.cli.run.workspace.git_source import (
    GIT_MIN_VERSION,
    parse_git_version,
)
from crewplane.cli.workflow_runner import execute_workflow_run
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import PreflightWorkflowSource
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
    workflow_payload_dict,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.workspace_cli_cleanup import assert_cleanup_dry_run
from tests.helpers.workspace_workflow_fixtures import (
    BASE_APP_TEXT,
    assert_workspace_e2e_artifacts,
    git_text,
    latest_succeeded_run,
    run_dirs,
    write_initial_failure_fixtures,
    write_success_fixtures,
)


class WorkspaceUnavailableArtifactsAdapter:
    create_store_calls = 0

    def canonicalize_options(
        self,
        implementation: str,
        resolved_identity: str,
        options: dict[str, Any] | None = None,
    ) -> CanonicalIntegrationConfig:
        return CanonicalIntegrationConfig(
            implementation=implementation,
            resolved_identity=resolved_identity,
            options=dict(options or {}),
        )

    def create_store(
        self,
        workflow_name: str,
        state_dir: Path,
        project_root: Path,
        options: dict[str, Any] | None = None,
    ) -> None:
        del workflow_name, state_dir, project_root, options
        type(self).create_store_calls += 1
        raise AssertionError("workspace real run must fail before store allocation")


async def run_workspace_enabled_mock_e2e(tmp_path: Path) -> None:
    project_root = _workspace_project(tmp_path)
    fixtures_dir = tmp_path / "fixtures"
    cache_root = tmp_path / "workspace-cache"
    config = _workspace_config(cache_root, fixtures_dir)
    workflow = _workspace_workflow()
    original_cwd = Path.cwd()
    resumed_run: Path | None = None
    os.chdir(project_root)
    try:
        write_initial_failure_fixtures(fixtures_dir)
        first_stream = io.StringIO()
        with pytest.raises(RuntimeError, match="valid canonical candidate"):
            await _run_workflow(workflow, config, Console(file=first_stream))

        write_success_fixtures(fixtures_dir)
        second_stream = io.StringIO()
        await _run_workflow(workflow, config, Console(file=second_stream))
        assert "Resuming workflow" in second_stream.getvalue()
        resumed_run = latest_succeeded_run(project_root)

        run_count_after_resume = len(run_dirs(project_root))
        duplicate_stream = io.StringIO()
        await _run_workflow(workflow, config, Console(file=duplicate_stream))
        assert len(run_dirs(project_root)) == run_count_after_resume
        assert "Identical context detected" in duplicate_stream.getvalue()

        force_stream = io.StringIO()
        await _run_workflow(workflow, config, Console(file=force_stream), force=True)
        assert len(run_dirs(project_root)) == run_count_after_resume + 1
    finally:
        os.chdir(original_cwd)

    assert resumed_run is not None
    assert_workspace_e2e_artifacts(resumed_run)
    assert_cleanup_dry_run(project_root, cache_root)


async def run_workspace_real_run_rejects_non_filesystem_artifacts(
    tmp_path: Path,
) -> None:
    project_root = _workspace_project(tmp_path)
    fixtures_dir = tmp_path / "fixtures"
    cache_root = tmp_path / "workspace-cache"
    config = _workspace_config(
        cache_root,
        fixtures_dir,
        artifact_implementation=(
            "tests.helpers.workspace_workflow_runner:"
            "WorkspaceUnavailableArtifactsAdapter"
        ),
    )
    workflow = _workspace_workflow()
    original_cwd = Path.cwd()
    WorkspaceUnavailableArtifactsAdapter.create_store_calls = 0
    os.chdir(project_root)
    try:
        with pytest.raises(RuntimeError, match="filesystem artifacts backend"):
            await _run_workflow(workflow, config, Console(file=io.StringIO()))
    finally:
        os.chdir(original_cwd)
    assert WorkspaceUnavailableArtifactsAdapter.create_store_calls == 0


def local_git_supports_workspace_policy() -> bool:
    try:
        version_text = subprocess.run(
            ["git", "--version"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
    version = parse_git_version(version_text)
    return version is not None and version >= GIT_MIN_VERSION


def _workspace_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    (project_root / "docs").mkdir(parents=True)
    (project_root / "src").mkdir()
    (project_root / "docs" / "input.md").write_text(
        "workspace requirements\n",
        encoding="utf-8",
    )
    (project_root / "src" / "app.txt").write_text(BASE_APP_TEXT, encoding="utf-8")
    (project_root / "workflow.task.md").write_text(
        "workspace workflow source\n",
        encoding="utf-8",
    )
    git_text(project_root, "init")
    git_text(project_root, "config", "user.name", "Crewplane Test")
    git_text(project_root, "config", "user.email", "crewplane-test@example.invalid")
    git_text(project_root, "add", "docs/input.md", "src/app.txt", "workflow.task.md")
    git_text(project_root, "commit", "-m", "initial")
    return project_root


def _workspace_config(
    cache_root: Path,
    fixtures_dir: Path,
    artifact_implementation: str = "filesystem",
) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="model-a")},
        settings=Settings(
            workspace={
                "enabled": True,
                "cache_root": cache_root.as_posix(),
                "cleanup_on_success": False,
            },
            integrations={
                "invoker": {
                    "implementation": "mock",
                    "options": {
                        "observation_delay_seconds": 0,
                        "output_mode": "file",
                        "output_dir": fixtures_dir.as_posix(),
                        "strict_file_mode": True,
                    },
                },
                "ui": {"implementation": "none", "options": {}},
                "artifacts": {
                    "implementation": artifact_implementation,
                    "options": {
                        "allowed_template_paths": [],
                        "log_cli_output": True,
                    },
                },
            },
        ),
    )


def _workspace_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="WorkspaceMockE2E",
        worktrees={
            "scratch": {"kind": "snapshot"},
            "implementation": {"kind": "worktree"},
        },
        nodes=[
            WorkflowNode(
                id="requirements",
                mode="input",
                source="{{file:docs/input.md}}",
            ),
            WorkflowNode(
                id="snapshot.read",
                mode="sequential",
                needs=["requirements"],
                worktree="scratch",
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=(
                            "Read requirements.\n"
                            "{{requirements.output}}\n"
                            "{{file:src/app.txt}}"
                        ),
                    )
                ],
            ),
            WorkflowNode(
                id="implement.review",
                mode="sequential",
                needs=["snapshot.read"],
                worktree="implementation",
                providers=[
                    ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="alpha", role=ProviderRole.REVIEWER),
                ],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=(
                            "Implement the change.\n"
                            "{{snapshot.read.output}}\n"
                            "{{file:src/app.txt}}"
                        ),
                    )
                ],
            ),
            WorkflowNode(
                id="lineage.consumer",
                mode="sequential",
                needs=["implement.review"],
                worktree="implementation",
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content=(
                            "Consume mutable lineage.\n"
                            "{{implement.review.output}}\n"
                            "{{file:src/app.txt}}"
                        ),
                    )
                ],
            ),
        ],
    )


async def _run_workflow(
    workflow: WorkflowPlan,
    config: Config,
    console: Console,
    force: bool = False,
) -> None:
    await execute_workflow_run(
        config=config,
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="workspace workflow source\n",
            composed_workflow=workflow_payload_dict(workflow),
            root_workflow_path=Path.cwd() / "workflow.task.md",
        ),
        force=force,
        no_live=True,
        console=console,
    )
