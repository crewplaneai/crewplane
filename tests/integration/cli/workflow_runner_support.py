from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from crewplane.cli.workflow_runner import (
    execute_workflow_run,
)
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    PreflightWorkflowSource,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability import ObservabilityHub
from crewplane.version import SCHEMA_VERSION


def mock_runner_config(
    options: dict[str, object] | None = None,
    invoker_implementation: str = "mock",
    artifact_implementation: str = "filesystem",
    artifact_options: dict[str, object] | None = None,
) -> Config:
    resolved_artifact_options = (
        {
            "log_cli_output": True,
        }
        if artifact_options is None
        else artifact_options
    )
    invoker_options = dict(options or {})
    if invoker_implementation == "mock":
        invoker_options = {
            "observation_delay_seconds": 0,
            "output_mode": "echo",
            **invoker_options,
        }
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["mock"], default_model="model-a")},
        settings=Settings(
            integrations={
                "invoker": {
                    "implementation": invoker_implementation,
                    "options": invoker_options,
                },
                "ui": {"implementation": "none", "options": {}},
                "artifacts": {
                    "implementation": artifact_implementation,
                    "options": resolved_artifact_options,
                },
            }
        ),
    )


def runner_workflow(prompt: str = "hello") -> WorkflowPlan:
    return WorkflowPlan(
        name="Task",
        nodes=[
            WorkflowNode(
                id="build.node",
                mode="sequential",
                providers=[ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR)],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content=prompt)
                ],
            )
        ],
    )


def runner_workflow_payload(workflow: WorkflowPlan) -> dict[str, object]:
    return {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description,
        "inputs": dict(workflow.inputs),
        "nodes": [],
    }


def run_directories(root: Path) -> list[Path]:
    stages_root = root / ".crewplane" / "execution-stages"
    if not stages_root.exists():
        return []
    return sorted(path for path in stages_root.iterdir() if path.is_dir())


def result_directories(root: Path) -> list[Path]:
    results_root = root / ".crewplane" / "execution-results"
    if not results_root.exists():
        return []
    return sorted(path for path in results_root.iterdir() if path.is_dir())


async def run_workflow(
    workflow: WorkflowPlan,
    config: Config,
    console: Console,
    force: bool = False,
    which_fn: Callable[[str], str | None] | None = None,
    execute_workflow_impl: Callable[..., Any] | None = None,
    observability_hub_cls: type[ObservabilityHub] | None = None,
) -> None:
    run_kwargs = {}
    if execute_workflow_impl is not None:
        run_kwargs["execute_workflow_impl"] = execute_workflow_impl
    if observability_hub_cls is not None:
        run_kwargs["observability_hub_cls"] = observability_hub_cls
    await execute_workflow_run(
        config=config,
        source=PreflightWorkflowSource.from_workflow(
            workflow,
            workflow_content="workflow source",
            composed_workflow=runner_workflow_payload(workflow),
            root_workflow_path=Path.cwd() / "workflow.task.md",
        ),
        force=force,
        no_live=True,
        console=console,
        which_fn=which_fn,
        **run_kwargs,
    )
