from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestrator_cli.architecture.contracts import (
    InvocationPlan,
    OutputExtractionMode,
    ProviderKind,
    QuotaParserProfile,
    StructuredOutputMode,
    UsageParserProfile,
)
from orchestrator_cli.core.config import AgentConfig


@dataclass(frozen=True)
class CliProviderCapability:
    provider_kind: ProviderKind
    structured_output_mode: StructuredOutputMode
    output_extraction_mode: OutputExtractionMode
    quota_parser: QuotaParserProfile
    usage_parser: UsageParserProfile
    model_arg: str | None = "--model"
    structured_output_args: tuple[str, ...] = ()


CAPABILITIES: dict[ProviderKind, CliProviderCapability] = {
    "claude": CliProviderCapability(
        provider_kind="claude",
        structured_output_mode="claude_json",
        output_extraction_mode="claude_json",
        quota_parser="claude",
        usage_parser="claude",
        structured_output_args=("--output-format", "json"),
    ),
    "codex": CliProviderCapability(
        provider_kind="codex",
        structured_output_mode="codex_last_message_file",
        output_extraction_mode="codex_last_message_file",
        quota_parser="codex",
        usage_parser="codex",
    ),
    "copilot": CliProviderCapability(
        provider_kind="copilot",
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="copilot",
        usage_parser="none",
    ),
    "gemini": CliProviderCapability(
        provider_kind="gemini",
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="gemini",
        usage_parser="none",
    ),
    "kilo": CliProviderCapability(
        provider_kind="kilo",
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="kilo",
        usage_parser="none",
    ),
    "generic": CliProviderCapability(
        provider_kind="generic",
        structured_output_mode="none",
        output_extraction_mode="visible",
        quota_parser="generic",
        usage_parser="none",
    ),
}


def get_cli_provider_capability(provider_kind: ProviderKind) -> CliProviderCapability:
    return CAPABILITIES[provider_kind]


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


def _build_argv(
    config: AgentConfig,
    capability: CliProviderCapability,
    model: str | None,
    prompt: str,
    structured_output_file: Path | None,
) -> list[str]:
    cmd = config.get_command()
    model_arg = (
        config.model_arg if config.provider_kind == "generic" else capability.model_arg
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
        prefix="orchestrator-codex-",
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
