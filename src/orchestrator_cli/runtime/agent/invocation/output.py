from __future__ import annotations

import codecs
import contextlib
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    CommandResult,
    OutputExtractionMode,
    UsageParserProfile,
)

from ..usage import (
    ParsedProviderUsage,
    output_text_for_usage,
    parse_provider_usage_from_result,
)
from .claude_json import extract_claude_json_result
from .state import (
    ExtractedInvocationOutput,
    InvocationAttemptResult,
    InvocationCommandRuntime,
    InvocationDiagnosticNotice,
)

STREAM_READ_BYTES = 65_536


def build_invocation_attempt_result(
    runtime: InvocationCommandRuntime,
    result: CommandResult,
) -> InvocationAttemptResult:
    extracted_output = _extract_successful_structured_output(
        output_extraction_mode=runtime.output_extraction_mode,
        usage_parser=runtime.usage_parser,
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
    output_extraction_mode: OutputExtractionMode,
    usage_parser: UsageParserProfile,
    cmd: list[str],
    result: CommandResult,
    structured_output_file: Path | None,
) -> ExtractedInvocationOutput:
    if output_extraction_mode == "codex_last_message_file":
        return _extract_codex_output(
            result=result,
            structured_output_file=structured_output_file,
            usage_parser=usage_parser,
        )
    if output_extraction_mode == "claude_json":
        return _extract_claude_output(result=result, usage_parser=usage_parser)
    return _extract_visible_output(
        cmd=cmd,
        result=result,
    )


def _extract_successful_structured_output(
    output_extraction_mode: OutputExtractionMode,
    usage_parser: UsageParserProfile,
    cmd: list[str],
    result: CommandResult,
    structured_output_file: Path | None,
) -> ExtractedInvocationOutput | None:
    if output_extraction_mode == "visible" or result.returncode != 0:
        return None
    extracted_output = extract_invocation_output(
        output_extraction_mode=output_extraction_mode,
        usage_parser=usage_parser,
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
        stdout_path=extracted_output.output_path,
        stderr_path=result.stderr_path,
    )


def _extract_codex_output(
    result: CommandResult,
    structured_output_file: Path | None,
    usage_parser: UsageParserProfile,
) -> ExtractedInvocationOutput:
    parsed_provider_usage = parse_provider_usage_from_result(usage_parser, result)
    if structured_output_file is None or not structured_output_file.exists():
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parsed_provider_usage,
        )
    if not _path_has_non_whitespace_text(structured_output_file):
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=parsed_provider_usage,
        )
    return ExtractedInvocationOutput(
        output_text="",
        output_extraction_status="success",
        parsed_provider_usage=parsed_provider_usage,
        output_path=structured_output_file,
        output_char_count=_path_decoded_character_count(structured_output_file),
    )


def _extract_claude_output(
    result: CommandResult,
    usage_parser: UsageParserProfile,
) -> ExtractedInvocationOutput:
    extraction = extract_claude_json_result(result)
    if extraction.output_extraction_status != "success":
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status=extraction.output_extraction_status,
            parsed_provider_usage=extraction.parsed_provider_usage,
        )
    if extraction.output_path is None or not _path_has_non_whitespace_text(
        extraction.output_path
    ):
        cleanup_extracted_invocation_output(extraction)
        return ExtractedInvocationOutput(
            output_text="",
            output_extraction_status="missing",
            parsed_provider_usage=extraction.parsed_provider_usage,
        )
    return ExtractedInvocationOutput(
        output_text="",
        output_extraction_status="success",
        parsed_provider_usage=extraction.parsed_provider_usage
        if usage_parser == "claude"
        else ParsedProviderUsage(status="none"),
        output_path=extraction.output_path,
        output_char_count=extraction.output_char_count,
        owns_output_path=extraction.owns_output_path,
    )


def _extract_visible_output(
    cmd: list[str],
    result: CommandResult,
) -> ExtractedInvocationOutput:
    output_text, output_path, output_char_count = _visible_stream_output(
        result.stdout_text,
        result.stdout_path,
    )
    notice: InvocationDiagnosticNotice | None = None
    if output_path is None and not output_text.strip():
        stderr_text, stderr_path, stderr_char_count = _visible_stream_output(
            result.stderr_text,
            result.stderr_path,
        )
        if stderr_path is not None or stderr_text.strip():
            output_text = stderr_text
            output_path = stderr_path
            output_char_count = stderr_char_count
            notice = InvocationDiagnosticNotice(
                level="warning",
                message=(
                    f"{cmd[0]} invocation succeeded with empty stdout; "
                    "using stderr as output. Provider log contains the original stderr "
                    "lines."
                ),
                operation="stderr_fallback",
                attributes={
                    "stderr_bytes": _stream_size_bytes(
                        result.stderr_text,
                        result.stderr_path,
                    ),
                    "stdout_bytes": _stream_size_bytes(
                        result.stdout_text,
                        result.stdout_path,
                    ),
                },
                console_message=(
                    "[yellow]WARN[/] "
                    f"{cmd[0]} returned output on stderr with empty stdout; "
                    "using stderr as output."
                ),
            )
    if output_path is None and not output_text.strip():
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
        output_path=output_path,
        output_char_count=output_char_count,
    )


def write_extracted_invocation_output(
    extracted_output: ExtractedInvocationOutput,
    output_file: Path,
) -> None:
    if extracted_output.output_path is None:
        output_file.write_text(extracted_output.output_text, encoding="utf-8")
        return
    _write_decoded_stream_file(extracted_output.output_path, output_file)


def cleanup_extracted_invocation_output(
    extracted_output: ExtractedInvocationOutput | None,
) -> None:
    if (
        extracted_output is None
        or not extracted_output.owns_output_path
        or extracted_output.output_path is None
    ):
        return
    with contextlib.suppress(OSError):
        extracted_output.output_path.unlink(missing_ok=True)


def _visible_stream_output(
    fallback_text: str,
    path: Path | None,
) -> tuple[str, Path | None, int | None]:
    if path is None or not path.is_file():
        return fallback_text, None, len(fallback_text)
    if not _path_has_non_whitespace_text(path):
        return "", None, 0
    return "", path, _path_decoded_character_count(path)


def _path_has_non_whitespace_text(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            if any(not char.isspace() for char in decoder.decode(chunk)):
                return True
    return any(not char.isspace() for char in decoder.decode(b"", final=True))


def _path_decoded_character_count(path: Path) -> int:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    char_count = 0
    with path.open("rb") as handle:
        while chunk := handle.read(STREAM_READ_BYTES):
            char_count += len(decoder.decode(chunk))
    return char_count + len(decoder.decode(b"", final=True))


def _stream_size_bytes(fallback_text: str, path: Path | None) -> int:
    if path is not None and path.is_file():
        return path.stat().st_size
    return len(fallback_text.encode("utf-8"))


def _write_decoded_stream_file(source: Path, destination: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    with (
        source.open("rb") as source_handle,
        destination.open("w", encoding="utf-8") as destination_handle,
    ):
        while chunk := source_handle.read(STREAM_READ_BYTES):
            destination_handle.write(decoder.decode(chunk))
        destination_handle.write(decoder.decode(b"", final=True))
