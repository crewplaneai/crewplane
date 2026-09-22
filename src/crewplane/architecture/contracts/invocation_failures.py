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
        self.last_non_quota_failure: InvocationFailureSummary | None = None
        super().__init__(f"{prefix}: {summary.format_for_error(log_file)}")


ADVICE_BY_KIND: dict[FailureKind, str] = {
    "provider_session_context_exhausted": (
        "Provider context filled during tool or file exploration. Split the "
        "workflow, narrow file scope, or reduce provider tool output."
    ),
    "initial_request_too_large": (
        "The initial resolved prompt or artifact input is too large. Use smaller "
        "inputs, findings artifacts, or token-budget fail-fast settings."
    ),
    "provider_output_limit_exceeded": (
        "The provider hit an output limit. Narrow the task, request a smaller "
        "answer, or use a model/profile with a larger output budget."
    ),
    "quota_or_rate_limit": (
        "The provider reported quota or rate limiting. Quota retries are independent "
        "of max_retries; check retry/reset details and provider account limits."
    ),
    "auth_or_permission": (
        "The provider CLI is unauthenticated or lacks required tool, file, or "
        "account permissions."
    ),
    "model_or_config_error": (
        "The provider rejected the configured model, profile, flag, or request "
        "configuration."
    ),
    "provider_transport_error": (
        "The provider CLI or network stream failed. Retry only when configured "
        "retry rules identify the condition as transient."
    ),
    "provider_tool_error": (
        "A provider-side tool call failed. Inspect the provider log for the "
        "specific tool and arguments."
    ),
    "malformed_provider_output": (
        "The provider did not emit the structured output crewplane requires."
    ),
    "provider_error": "The provider CLI reported an error.",
    "unknown_provider_error": "The provider CLI failed; inspect the invocation log.",
}
