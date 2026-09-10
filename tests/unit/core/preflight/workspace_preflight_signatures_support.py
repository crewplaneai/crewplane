from __future__ import annotations

from pathlib import Path

from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig, Config, Settings
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.preflight.models import (
    WorkspaceSourceSnapshot,
)
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.version import SCHEMA_VERSION


def project_root_workflow(
    model: str | None = None,
    provider_name: str = "alpha",
) -> WorkflowPlan:
    provider = (
        ProviderSpec(provider=provider_name, model=model)
        if model is not None
        else ProviderSpec(provider=provider_name)
    )
    return WorkflowPlan(
        name="project root workflow",
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                providers=[provider],
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="run")
                ],
            )
        ],
    )


def workspace_signature_config(workspace: dict[str, object]) -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={"alpha": AgentConfig(cli_cmd=["echo"])},
        settings=Settings(workspace=workspace),
    )


def compile_signature_workflow(
    root: Path,
    workflow: WorkflowPlan,
    config: Config,
    source_snapshot: WorkspaceSourceSnapshot | None = None,
):
    return compile_source_with_source_snapshot(
        root,
        PreflightWorkflowSource.from_workflow(workflow),
        source_snapshot,
        config,
    )


def compile_source_with_source_snapshot(
    root: Path,
    source: PreflightWorkflowSource,
    source_snapshot: WorkspaceSourceSnapshot | None,
    config: Config | None = None,
):
    resolved_config = config or workspace_signature_config({"enabled": True})
    runtime_snapshot = build_runtime_config_snapshot(
        config=resolved_config,
        console=Console(file=None),
        no_live=True,
    )
    return compile_preflight_preview(
        source=source,
        config=resolved_config,
        runtime_snapshot=runtime_snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
            workspace_source_snapshot=source_snapshot,
        ),
    )
