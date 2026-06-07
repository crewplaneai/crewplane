from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol

from orchestrator_cli.core.config import AgentConfig

if TYPE_CHECKING:
    from .usage import InvocationUsage


class AgentInvoker(Protocol):
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        """Invoke an agent and write the output to a file."""


RuntimeLogValue = str | int | float | bool | None
InvocationLogLevel = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class InvocationDiagnostic:
    level: InvocationLogLevel
    message: str
    operation: str
    attributes: Mapping[str, RuntimeLogValue] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None:
            object.__setattr__(
                self,
                "attributes",
                MappingProxyType(dict(self.attributes)),
            )


InvocationDiagnosticSink = Callable[[InvocationDiagnostic], None]
InvocationUsageSink = Callable[["InvocationUsage"], None]
ConsoleMessageSink = Callable[[str], None]


@dataclass(frozen=True)
class InvocationContext:
    node_id: str
    task_id: str
    provider: str
    role: str
    audit_round_num: int | None = None
    round_num: int | None = None
    findings_enabled: bool = False
    diagnostics: InvocationDiagnosticSink | None = None
    usage_recorder: InvocationUsageSink | None = None
    console_message_sink: ConsoleMessageSink | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout_text: str
    stderr_text: str

    @property
    def combined_output(self) -> str:
        return f"{self.stderr_text}\n{self.stdout_text}"


@dataclass(frozen=True)
class QuotaClassification:
    is_quota: bool
    reset_after_seconds: float | None
    evidence: str | None


class CommandRunner(Protocol):
    async def __call__(
        self,
        cmd: list[str],
        stdin_data: bytes | None,
        log_file: Path | None,
        append_log: bool,
        log_header: bytes | None,
        invocation_context: InvocationContext | None,
        idle_timeout_seconds: float | None,
    ) -> CommandResult: ...
