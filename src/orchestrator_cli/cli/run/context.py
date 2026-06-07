from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.config import Config
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.workflow_models import WorkflowPlan

from ..paths import ORCHESTRATOR_DIR, get_orchestrator_dir


@dataclass(frozen=True)
class WorkflowRunContext:
    config: Config
    source: PreflightWorkflowSource
    console: Console
    project_root: Path
    orchestrator_dir: Path

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


def resolve_orchestrator_dir(project_root: Path, orchestrator_dir: Path | None) -> Path:
    if orchestrator_dir is not None:
        return orchestrator_dir.resolve(strict=False)
    if project_root != Path.cwd():
        return project_root / ORCHESTRATOR_DIR
    return get_orchestrator_dir()
