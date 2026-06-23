from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.ports.artifacts import StageTaskSpec
from crewplane.core.preflight.models import (
    PreflightExecutionNode,
    ProviderRecord,
)
from crewplane.runtime.execution.workspace_files import ResolvedWorkspaceFile


@dataclass(frozen=True)
class ParallelInvocation:
    provider: ProviderRecord
    prompt: str
    output_file: Path
    task_id: str
    workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


@dataclass(frozen=True)
class ParallelResultSummary:
    total: int
    successful: int
    failed: int


def build_stage_task_specs(node: PreflightExecutionNode) -> tuple[StageTaskSpec, ...]:
    if node.mode == "input":
        return ()

    return tuple(
        StageTaskSpec(
            task_id=provider.task_id,
            role=provider.role,
        )
        for provider in node.provider_records
    )
