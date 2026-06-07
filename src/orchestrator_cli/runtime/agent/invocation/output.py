from __future__ import annotations

import json
from pathlib import Path

from ..types import CommandResult
from ..usage import (
    OutputExtractionStatus,
    ParsedProviderUsage,
    ProviderKind,
    output_text_for_usage,
    parse_provider_usage,
)
from .state import (
    ExtractedInvocationOutput,
    InvocationAttemptResult,
    InvocationCommandRuntime,
    InvocationDiagnosticNotice,
)


def build_invocation_attempt_result(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
) -> InvocationAttemptResult:
    extracted_output = _extract_successful_structured_output(
        provider_kind=runtime.provider_kind,
        cmd=runtime.cmd,
        result=result,
        structured_output_file=runtime.structured_output_file,
    )
    if extracted_output is not None:
        return InvocationAttemptResult(
            result=_retry_result_from_extracted_output(
                extracted_output=extracted_output,
                result=result,
            ),
            extracted_output=extracted_output,
            usage_output=extracted_output.output_text,
        )
    return InvocationAttemptResult(
        result=result,
        extracted_output=None,
        usage_output=output_text_for_usage(result),
    )


def extract_invocation_output(
    provider_kind: ProviderKind,
    cmd: list[str],
    result: CommandResult,
    structured_output_file: Path | None,
) -> ExtractedInvocationOutput:
    if provider_kind == "codex":
        return _extract_codex_output(
            result=result,
            structured_output_file=structured_output_file,
        )
    if provider_kind == "claude":
        return _extract_claude_output(result=result)
    return _extract_visible_output(
        cmd=cmd,
        result=result,
    )


def _extract_successful_structured_output(
    provider_kind: ProviderKind,
    cmd: list[str],
    result: CommandResult,
    structured_output_file: Path | None,
) -> ExtractedInvocationOutput | None:
    if provider_kind not in {"codex", "claude"} or result.returncode != 0:
        return None
    extracted_output = extract_invocation_output(
        provider_kind=provider_kind,
        cmd=cmd,
        result=result,
        structured_output_file=structured_output_file,
    )
    if extracted_output.output_extraction_status != "success":
        return None
    return extracted_output


def _retry_result_from_extracted_output(
    extracted_output: ExtractedInvocationOutput,
    result: CommandResult,
) -> CommandResult:
    return CommandResult(
        returncode=0,
        stdout_text=extracted_output.output_text,
        stderr_text=result.stderr_text,
    )


def _extract_codex_output(
    result: CommandResult,
    structured_output_file: Path | None,
) -> ExtractedInvocationOutput:
    parsed_provider_usage = parse_provider_usage(
        "codex",
        result.stdout_text,
        result.stderr_text,
    )
    if structured_output_file is None or not structured_output_file.exists():
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parsed_provider_usage,
        )
    try:
        output_text = structured_output_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="malformed",
            parsed_provider_usage=parsed_provider_usage,
        )
    if not output_text.strip():
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parsed_provider_usage,
        )
    return ExtractedInvocationOutput(
        output_text=output_text,
        output_extraction_status="success",
        parsed_provider_usage=parsed_provider_usage,
    )


def _extract_claude_output(result: CommandResult) -> ExtractedInvocationOutput:
    payload, extraction_status = _load_structured_json_payload(result)
    if payload is None:
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status=extraction_status,
            parsed_provider_usage=ParsedProviderUsage(status="none"),
        )
    result_value = payload.get("result")
    if not isinstance(result_value, str):
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="malformed"
            if result_value is not None
            else "missing",
            parsed_provider_usage=parse_provider_usage(
                "claude",
                result.stdout_text,
                result.stderr_text,
            ),
        )
    if not result_value.strip():
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parse_provider_usage(
                "claude",
                result.stdout_text,
                result.stderr_text,
            ),
        )
    return ExtractedInvocationOutput(
        output_text=result_value,
        output_extraction_status="success",
        parsed_provider_usage=parse_provider_usage(
            "claude",
            result.stdout_text,
            result.stderr_text,
        ),
    )


def _load_structured_json_payload(
    result: CommandResult,
) -> tuple[dict[str, object] | None, OutputExtractionStatus]:
    for text in (result.stdout_text, result.stderr_text):
        stripped = text.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None, "malformed"
        if not isinstance(payload, dict):
            return None, "malformed"
        return payload, "success"
    return None, "missing"


def _extract_visible_output(
    cmd: list[str],
    result: CommandResult,
) -> ExtractedInvocationOutput:
    output_text = result.stdout_text
    notice: InvocationDiagnosticNotice | None = None
    if not output_text.strip() and result.stderr_text.strip():
        output_text = result.stderr_text
        notice = InvocationDiagnosticNotice(
            level="warning",
            message=(
                f"{cmd[0]} invocation succeeded with empty stdout; "
                "using stderr as output. Provider log contains the original stderr "
                "lines."
            ),
            operation="stderr_fallback",
            attributes={
                "stderr_bytes": len(result.stderr_text.encode("utf-8")),
                "stdout_bytes": len(result.stdout_text.encode("utf-8")),
            },
            console_message=(
                "[yellow]WARN[/] "
                f"{cmd[0]} returned output on stderr with empty stdout; "
                "using stderr as output."
            ),
        )
    if not output_text.strip():
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=ParsedProviderUsage(status="none"),
            notice=notice,
        )
    return ExtractedInvocationOutput(
        output_text=output_text,
        output_extraction_status="success",
        parsed_provider_usage=ParsedProviderUsage(status="none"),
        notice=notice,
    )
