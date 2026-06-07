from __future__ import annotations

from pathlib import Path
from typing import Literal

from orchestrator_cli.core.config import AgentConfig

ProviderKind = Literal["claude", "codex", "copilot", "gemini", "kilo", "generic"]


def matches_any(haystack: str, needles: list[str]) -> bool:
    if not haystack or not needles:
        return False
    haystack_lower = haystack.lower()
    return any(needle and needle.lower() in haystack_lower for needle in needles)


def normalize_cli_executable(cli_executable: str) -> str:
    executable = Path(cli_executable).name.lower()
    if executable.endswith(".exe"):
        return executable[:-4]
    return executable


def provider_kind(cli_executable: str) -> ProviderKind:
    normalized = normalize_cli_executable(cli_executable)
    if normalized == "claude":
        return "claude"
    if normalized == "codex":
        return "codex"
    if normalized == "copilot":
        return "copilot"
    if normalized == "gemini":
        return "gemini"
    if normalized in {"kilo", "kilocode"}:
        return "kilo"
    return "generic"


def _effective_model_arg(config: AgentConfig, cli_executable: str) -> str | None:
    if config.model_arg is not None:
        return config.model_arg
    if normalize_cli_executable(cli_executable) == "gemini":
        return "--model"
    return None


def _effective_prompt_arg(config: AgentConfig, cli_executable: str) -> str | None:
    resolved_provider_kind = provider_kind(cli_executable)
    if resolved_provider_kind == "claude":
        return "-p"
    if config.prompt_arg is not None:
        return config.prompt_arg
    if config.use_stdin:
        return None
    if normalize_cli_executable(cli_executable) == "gemini":
        return "--prompt"
    return None


def build_command(
    config: AgentConfig,
    model: str | None,
    prompt: str,
    structured_output_file: Path | None = None,
) -> list[str]:
    cmd = config.get_command()
    cli_executable = cmd[0]
    resolved_provider_kind = provider_kind(cli_executable)
    model_arg = _effective_model_arg(config, cli_executable)
    if model_arg is not None and model is not None:
        cmd.extend([model_arg, model])

    if config.extra_args:
        cmd.extend(config.extra_args)

    if resolved_provider_kind == "codex":
        if structured_output_file is None:
            raise ValueError("Codex invocations require a structured_output_file path.")
        cmd.extend(["--json", "--output-last-message", str(structured_output_file)])
    elif resolved_provider_kind == "claude":
        cmd.extend(["--output-format", "json"])

    if config.use_stdin:
        if config.stdin_prompt_arg:
            cmd.append(config.stdin_prompt_arg)
        return cmd

    prompt_arg = _effective_prompt_arg(config, cli_executable)
    if prompt_arg:
        cmd.extend([prompt_arg, prompt])
    else:
        cmd.append(prompt)
    return cmd
