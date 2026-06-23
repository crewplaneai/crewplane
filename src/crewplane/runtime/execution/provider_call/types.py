from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from crewplane.architecture.contracts import AgentInvoker
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.core.preflight.models import ProviderRecord
from crewplane.core.workflow.keywords import ProviderRole

from ..activity.telemetry import ExecutionTelemetry
from ..runtime_context import CompiledRuntimeContext
from ..workspace_files import ResolvedWorkspaceFile


class ProviderOutputPolicy(StrEnum):
    REQUIRE_OUTPUT = "require_output"
    ALLOW_MISSING_OUTPUT = "allow_missing_output"


@dataclass(frozen=True)
class ProviderCallRequest:
    runtime_context: CompiledRuntimeContext
    output: ArtifactStorePort
    node_id: str
    provider: ProviderRecord
    task_id: str
    audit_round_num: int | None
    round_num: int
    prompt: str
    output_file: Path
    role_label: ProviderRole
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    findings_enabled: bool = False
    provider_output_policy: ProviderOutputPolicy = ProviderOutputPolicy.REQUIRE_OUTPUT
    on_log_file_resolved: Callable[[Path], None] | None = None
    rendered_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


@dataclass(frozen=True)
class ProviderCallResult:
    output_file: Path
    error: Exception | None = None
