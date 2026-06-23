from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from crewplane.architecture.contracts import (
    InvocationPlan,
    LogPresentationDescriptor,
    LogPresentationFormat,
    OutputExtractionMode,
    ProviderKind,
    QuotaParserProfile,
    StructuredOutputMode,
    UsageParserProfile,
)
from crewplane.core.config import AgentConfig


@dataclass(frozen=True)
class CliProviderCapability:
    provider_kind: ProviderKind
    structured_output_mode: StructuredOutputMode
    output_extraction_mode: OutputExtractionMode
    quota_parser: QuotaParserProfile
    usage_parser: UsageParserProfile
    log_presentation_format: LogPresentationFormat
    log_presentation_profile: str
    model_arg: str | None = "--model"
    structured_output_args: tuple[str, ...] = ()


CAPABILITIES: dict[ProviderKind, CliProviderCapability] = {
    ProviderKind.CLAUDE: CliProviderCapability(
        provider_kind=ProviderKind.CLAUDE,
        structured_output_mode="claude_json",
        output_extraction_mode="claude_json",
        quota_parser="claude",
        usage_parser="claude",
        log_presentation_format="json_object",
        log_presentation_profile="claude",
        structured_output_args=("--output-format", "json"),
    ),
    ProviderKind.CODEX: CliProviderCapability(
        provider_kind=ProviderKind.CODEX,
        structured_output_mode="codex_last_message_file",
        output_extraction_mode="codex_last_message_file",
        quota_parser="codex",
        usage_parser="codex",
        log_presentation_format="json_lines",
        log_presentation_profile="codex",
    ),
    ProviderKind.COPILOT: CliProviderCapability(
        provider_kind=ProviderKind.COPILOT,
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="copilot",
        usage_parser="none",
        log_presentation_format="plain",
        log_presentation_profile="generic",
    ),
    ProviderKind.GEMINI: CliProviderCapability(
        provider_kind=ProviderKind.GEMINI,
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="gemini",
        usage_parser="none",
        log_presentation_format="plain",
        log_presentation_profile="generic",
    ),
    ProviderKind.KILO: CliProviderCapability(
        provider_kind=ProviderKind.KILO,
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="kilo",
        usage_parser="none",
        log_presentation_format="plain",
        log_presentation_profile="generic",
    ),
    ProviderKind.GENERIC: CliProviderCapability(
        provider_kind=ProviderKind.GENERIC,
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="generic",
        usage_parser="none",
        log_presentation_format="plain",
        log_presentation_profile="generic",
    ),
}


def get_cli_provider_capability(provider_kind: ProviderKind) -> CliProviderCapability:
    return CAPABILITIES[ProviderKind(provider_kind)]


def build_cli_invocation_plan(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    output_file: Path,
) -> InvocationPlan:
    capability = get_cli_provider_capability(config.provider_kind)
    structured_output_file = _structured_output_file(capability)
    cmd = _build_argv(config, capability, model, prompt, structured_output_file)
    stdin_data = prompt.encode("utf-8") if config.prompt_transport == "stdin" else None
    return InvocationPlan(
        cmd=cmd,
        stdin_data=stdin_data,
        structured_output_file=structured_output_file,
        structured_output_mode=capability.structured_output_mode,
        output_extraction_mode=capability.output_extraction_mode,
        quota_parser=capability.quota_parser,
        usage_parser=capability.usage_parser,
        failure_profile=capability.provider_kind,
        log_provider_kind=capability.provider_kind,
        log_header=_build_log_header(
            cli_executable=cmd[0],
            model=model,
            output_file=output_file,
        ),
    )


def build_cli_log_presentation(config: AgentConfig) -> LogPresentationDescriptor:
    capability = get_cli_provider_capability(config.provider_kind)
    return LogPresentationDescriptor(
        format=capability.log_presentation_format,
        profile=capability.log_presentation_profile,
    )


def _build_argv(
    config: AgentConfig,
    capability: CliProviderCapability,
    model: str | None,
    prompt: str,
    structured_output_file: Path | None,
) -> list[str]:
    """Build the provider CLI argv and validate the executable.

    The first argument identifies the process to launch, so it is resolved
    before appending provider flags. This keeps command validation and log
    headers tied to the actual executable while leaving user-supplied
    arguments untouched.
    """
    cmd = config.get_command()
    cmd[0] = _resolved_cli_executable(cmd[0])
    model_arg = (
        config.model_arg
        if config.provider_kind == ProviderKind.GENERIC
        else capability.model_arg
    )
    if model_arg is not None and model is not None:
        cmd.extend([model_arg, model])
    cmd.extend(config.extra_args)
    cmd.extend(_structured_output_args(capability, structured_output_file))
    if config.prompt_transport == "stdin":
        if config.prompt_transport_arg:
            cmd.append(config.prompt_transport_arg)
        return cmd
    if config.prompt_transport_arg is None:
        raise ValueError("prompt_transport_arg is required for argv prompt transport.")
    cmd.extend([config.prompt_transport_arg, prompt])
    return cmd


def _resolved_cli_executable(executable: str) -> str:
    """Return an executable path suitable for subprocess invocation.

    Bare executable names are resolved through `PATH` and validated. Absolute
    paths are validated directly. Relative path-like commands are preserved so
    subprocess can resolve them relative to the configured working directory.
    """
    executable_path = Path(executable)
    if executable_path.is_absolute():
        return _resolved_existing_executable(executable_path)
    if _contains_path_separator(executable):
        return executable
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(f"CLI executable '{executable}' was not found.")
    return _resolved_existing_executable(Path(resolved))


def _resolved_existing_executable(executable: Path) -> str:
    """Resolve an existing executable path and fail clearly if it is unusable."""
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"CLI executable '{executable.as_posix()}' is not a file."
        )
    if not os.access(resolved, os.X_OK):
        raise PermissionError(
            f"CLI executable '{resolved.as_posix()}' is not executable."
        )
    return resolved.as_posix()


def _contains_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


def _structured_output_args(
    capability: CliProviderCapability,
    structured_output_file: Path | None,
) -> tuple[str, ...]:
    if capability.structured_output_mode == "codex_last_message_file":
        if structured_output_file is None:
            raise ValueError("Codex invocations require a structured output file.")
        return ("--json", "--output-last-message", str(structured_output_file))
    return capability.structured_output_args


def _structured_output_file(capability: CliProviderCapability) -> Path | None:
    if capability.structured_output_mode != "codex_last_message_file":
        return None
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix="crewplane-codex-",
        suffix=".last-message.txt",
    )
    os.close(file_descriptor)
    Path(temp_path).unlink(missing_ok=True)
    return Path(temp_path)


def _build_log_header(
    cli_executable: str,
    model: str | None,
    output_file: Path,
) -> bytes:
    started_at = datetime.now(UTC).isoformat()
    model_label = model if model is not None else "provider default"
    header = (
        f"started_at: {started_at}\n"
        f"cli_executable: {cli_executable}\n"
        f"model: {model_label}\n"
        f"output_file: {output_file}\n"
        "---\n"
    )
    return header.encode("utf-8")
