from __future__ import annotations

from .types import FailureKind

FAILURE_SUMMARY_MAX_CHARS = 240
MAX_FAILURE_LINES = 1_000
MAX_JSON_LINE_CHARS = 128_000
FAILURE_SUMMARY_HINTS: tuple[str, ...] = (
    "error",
    "failed",
    "exception",
    "quota",
    "rate limit",
    "429",
    "capacity",
    "resource exhausted",
    "timeout",
    "denied",
    "invalid",
    "not found",
    "context window",
    "too long",
    "max_output_tokens",
)
FAILURE_SUMMARY_NOISE_PREFIXES: tuple[str, ...] = (
    "yolo mode is enabled",
    "loaded cached credentials",
)
JSON_FAILURE_MARKERS: tuple[str, ...] = (
    '"type":"turn.failed"',
    '"type": "turn.failed"',
    '"type":"error"',
    '"type": "error"',
    '"is_error"',
    '"error"',
    '"message"',
)
PROVIDER_ERROR_EVENT_TYPES: tuple[str, ...] = ("error", "failed")
KIND_PRIORITY: dict[FailureKind, int] = {
    "provider_session_context_exhausted": 1_000,
    "initial_request_too_large": 950,
    "provider_output_limit_exceeded": 850,
    "auth_or_permission": 830,
    "model_or_config_error": 810,
    "quota_or_rate_limit": 790,
    "malformed_provider_output": 760,
    "provider_tool_error": 650,
    "provider_transport_error": 600,
    "provider_error": 400,
    "unknown_provider_error": 0,
}
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
        "The provider reported quota or rate limiting. Check retry/reset details "
        "and provider account limits."
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
        "The provider did not emit the structured output orchestrator requires."
    ),
    "provider_error": "The provider CLI reported an error.",
    "unknown_provider_error": "The provider CLI failed; inspect the invocation log.",
}

AUTH_OR_PERMISSION_PATTERNS: tuple[str, ...] = (
    "unauthenticated",
    "authentication failed",
    "not authenticated",
    "login required",
    "permission denied",
    "access denied",
    "not authorized",
    "forbidden",
)
MODEL_OR_CONFIG_PATTERNS: tuple[str, ...] = (
    "unknown model",
    "model not found",
    "unsupported model",
    "invalid model",
    "model is not available",
    "unknown option",
    "unrecognized option",
    "invalid argument",
)
INITIAL_REQUEST_TOO_LARGE_PATTERNS: tuple[str, ...] = (
    "prompt is too long",
    "request too large",
    "request is too large",
    "request exceeds",
    "input is too long",
    "input too long",
    "input token count",
    "input exceeds",
    "prompt exceeds",
)
PROVIDER_SESSION_CONTEXT_PATTERNS: tuple[str, ...] = (
    "ran out of room",
    "context window",
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
    "conversation too long",
    "conversation is too long",
    "clear earlier history",
    "start a new thread",
    "transcript too long",
    "too much context",
    "compaction failed",
    "error during compaction",
)
OUTPUT_LIMIT_PATTERNS: tuple[str, ...] = (
    "max_output_tokens",
    "output limit",
    "maximum output",
    "response length",
    "completion length",
    "finish reason: length",
    "finish_reason=length",
    'finish_reason":"length',
    "max tokens",
)
TRANSPORT_ERROR_PATTERNS: tuple[str, ...] = (
    "stream disconnected",
    "socket connection was closed unexpectedly",
    "connection was closed unexpectedly",
    "connection reset",
    "connection aborted",
    "network error",
    "timed out",
    "timeout",
    "reconnecting",
)
TOOL_ERROR_PATTERNS: tuple[str, ...] = (
    "mcp_tool_call",
    "tool call",
    "tool failed",
    "server must be provided",
    "failed to parse tool",
)
MALFORMED_OUTPUT_PATTERNS: tuple[str, ...] = (
    "malformed structured output",
    "invalid json",
    "failed to parse json",
    "jsondecodeerror",
)
