from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FailureKind = Literal[
    "provider_session_context_exhausted",
    "initial_request_too_large",
    "provider_output_limit_exceeded",
    "quota_or_rate_limit",
    "auth_or_permission",
    "model_or_config_error",
    "provider_transport_error",
    "provider_tool_error",
    "malformed_provider_output",
    "provider_error",
    "unknown_provider_error",
]
FailurePhase = Literal[
    "initial_request",
    "provider_session",
    "provider_output",
    "provider_transport",
    "provider_tool",
    "provider_config",
    "unknown",
]
FailureSource = Literal[
    "stdout_json",
    "stderr_json",
    "stdout_text",
    "stderr_text",
    "none",
]


@dataclass(frozen=True)
class InvocationFailureSummary:
    kind: FailureKind
    phase: FailurePhase
    source: FailureSource
    message: str
    advice: str
    condensed: bool

    def format_for_error(self, log_file: Path | None) -> str:
        if self.condensed and log_file is not None:
            return f"{self.message} (see {log_file})"
        return self.message


class InvocationFailureError(RuntimeError):
    def __init__(
        self,
        prefix: str,
        summary: InvocationFailureSummary,
        log_file: Path | None,
    ) -> None:
        self.summary = summary
        self.kind = summary.kind
        self.phase = summary.phase
        self.source = summary.source
        self.advice = summary.advice
        self.log_file = log_file
        super().__init__(f"{prefix}: {summary.format_for_error(log_file)}")


@dataclass(frozen=True)
class FailureEvidence:
    summary: InvocationFailureSummary
    priority: int
    sequence: int
