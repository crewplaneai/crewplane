from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.contracts import AgentInvoker
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import ProviderRecord

from .execution_activity import ExecutionTelemetry
from .runtime_context import CompiledRuntimeContext
from .workspace_files import ResolvedWorkspaceFile


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
    role_label: str
    invoker: AgentInvoker
    telemetry: ExecutionTelemetry | None
    findings_enabled: bool = False
    on_log_file_resolved: Callable[[Path], None] | None = None
    rendered_workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


@dataclass(frozen=True)
class ProviderCallResult:
    output_file: Path
    error: Exception | None = None
