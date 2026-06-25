from __future__ import annotations

from collections import Counter
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

    display_names = provider_display_names(node.provider_records)
    return tuple(
        StageTaskSpec(
            task_id=provider.task_id,
            role=provider.role,
            display_name=display_names[provider.task_id],
        )
        for provider in node.provider_records
    )


def provider_display_names(
    provider_records: list[ProviderRecord],
) -> dict[str, str]:
    display_keys = [provider_display_key(provider) for provider in provider_records]
    duplicate_counts = Counter(display_keys)
    occurrences: Counter[tuple[str, str]] = Counter()
    names: dict[str, str] = {}
    for provider, display_key in zip(provider_records, display_keys, strict=True):
        occurrences[display_key] += 1
        names[provider.task_id] = provider_display_name(
            display_key,
            occurrences[display_key],
            duplicate_counts[display_key],
        )
    return names


def provider_display_key(provider: ProviderRecord) -> tuple[str, str]:
    provider_name = " ".join(provider.provider.split()) or provider.task_id
    return provider_name, provider.role.value


def provider_display_name(
    display_key: tuple[str, str],
    occurrence: int,
    duplicate_count: int,
) -> str:
    provider_name, role_name = display_key
    if duplicate_count == 1:
        return f"{provider_name} ({role_name})"
    return f"{provider_name} ({role_name} {occurrence})"
