from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.config import Config
from crewplane.core.preflight.source import PreflightWorkflowSource
from crewplane.core.state_paths import (
    STATE_DIR_NAME,
    get_state_dir,
)
from crewplane.core.workflow.models import WorkflowPlan


@dataclass(frozen=True)
class WorkflowRunContext:
    config: Config
    source: PreflightWorkflowSource
    console: Console
    project_root: Path
    state_dir: Path

    @property
    def workflow(self) -> WorkflowPlan:
        return self.source.workflow


def print_artifact_locations(
    workflow_name: str,
    output: ArtifactStorePort,
    console: Console,
) -> None:
    console.print(f"Workflow: {workflow_name}", style="dim")
    console.print(f"Run ID: {output.run_id}", style="dim")
    console.print(f"Artifact key: {output.task_name}", style="dim")
    console.print(f"Stages: {output.stages_dir}", style="dim")
    console.print(f"Results: {output.results_dir}", style="dim")
    console.print(f"Logs: {output.logs_dir}", style="dim")


def fallback_workflow_name(tasks_file: Path) -> str:
    name = tasks_file.stem
    if name.endswith(".task"):
        name = name[: -len(".task")]
    return name or "invalid-workflow"


def resolve_project_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return project_root.resolve(strict=False)
    return Path.cwd()


def resolve_state_dir(project_root: Path, state_dir: Path | None) -> Path:
    if state_dir is not None:
        return state_dir.resolve(strict=False)
    if project_root != Path.cwd():
        return project_root / STATE_DIR_NAME
    return get_state_dir()
