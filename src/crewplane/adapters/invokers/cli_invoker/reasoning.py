from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from crewplane.architecture.contracts import ProviderKind
from crewplane.core.config import AgentConfig

from .env_command import parse_env_command_context

CODEX_REASONING_KEY = "model_reasoning_effort"
CLAUDE_REASONING_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
SUPPORTED_REASONING_PROVIDER_KINDS = frozenset(
    {ProviderKind.CODEX, ProviderKind.CLAUDE}
)


def validate_reasoning_request(
    config: AgentConfig,
    requested_reasoning: str | None,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
) -> None:
    if requested_reasoning is None:
        return
    provider_kind = ProviderKind(config.provider_kind)
    if provider_kind not in SUPPORTED_REASONING_PROVIDER_KINDS:
        raise ValueError(
            "First-class reasoning requires the built-in CLI invoker with "
            "provider_kind 'codex' or 'claude'."
        )
    effective_environment = os.environ if environment is None else environment
    cli_context = parse_env_command_context(
        config.cli_cmd,
        effective_environment.get(CLAUDE_REASONING_ENV, ""),
        CLAUDE_REASONING_ENV,
    )
    cli_arguments = cli_context.command_arguments
    effective_reasoning_environment = cli_context.tracked_environment_value
    _reject_cli_command_terminator(cli_arguments)
    if provider_kind == ProviderKind.CODEX:
        _reject_codex_reasoning_conflict(cli_arguments)
        _reject_codex_reasoning_conflict(config.extra_args)
        return
    _reject_claude_reasoning_conflict(cli_arguments, working_directory)
    _reject_claude_reasoning_conflict(config.extra_args, working_directory)
    if effective_reasoning_environment.strip():
        raise ValueError(
            f"{CLAUDE_REASONING_ENV} conflicts with the workflow reasoning request."
        )


def build_reasoning_args(
    config: AgentConfig,
    requested_reasoning: str | None,
    working_directory: Path | None = None,
) -> tuple[str, ...]:
    validate_reasoning_request(
        config,
        requested_reasoning,
        working_directory=working_directory,
    )
    if requested_reasoning is None:
        return ()
    provider_kind = ProviderKind(config.provider_kind)
    if provider_kind == ProviderKind.CODEX:
        return ("--config", f'{CODEX_REASONING_KEY}="{requested_reasoning}"')
    return ("--effort", requested_reasoning)


def _reject_cli_command_terminator(tokens: Sequence[str]) -> None:
    if "--" in tokens:
        raise ValueError(
            "A first-class reasoning request cannot be appended after the "
            "cli_cmd option terminator."
        )


def _reject_codex_reasoning_conflict(tokens: Sequence[str]) -> None:
    for assignment in _codex_config_assignments(tokens):
        key = assignment.partition("=")[0].strip()
        if key == CODEX_REASONING_KEY:
            raise ValueError(
                f"{CODEX_REASONING_KEY} conflicts with the workflow reasoning request."
            )


def _codex_config_assignments(tokens: Sequence[str]) -> tuple[str, ...]:
    assignments: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        assignment, consumed = _codex_config_assignment(tokens, index)
        if assignment is not None:
            assignments.append(assignment)
        index += consumed
    return tuple(assignments)


def _codex_config_assignment(
    tokens: Sequence[str],
    index: int,
) -> tuple[str | None, int]:
    token = tokens[index]
    if token in {"--config", "-c"}:
        if index + 1 >= len(tokens) or tokens[index + 1] == "--":
            raise ValueError(f"{token} requires a TOML assignment.")
        return _require_toml_assignment(tokens[index + 1], token), 2
    if token.startswith("--config="):
        return _require_toml_assignment(token.removeprefix("--config="), "--config"), 1
    if token.startswith("-c="):
        return _require_toml_assignment(token.removeprefix("-c="), "-c"), 1
    if token.startswith("-c") and token != "-c":
        return _require_toml_assignment(token[2:], "-c"), 1
    return None, 1


def _require_toml_assignment(value: str, option: str) -> str:
    if "=" not in value or not value.partition("=")[0].strip():
        raise ValueError(f"{option} requires a TOML key=value assignment.")
    return value


def _reject_claude_reasoning_conflict(
    tokens: Sequence[str],
    working_directory: Path | None,
) -> None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return
        if token == "--effort":
            if index + 1 >= len(tokens) or tokens[index + 1] == "--":
                raise ValueError("--effort requires a value.")
            raise ValueError("--effort conflicts with the workflow reasoning request.")
        if token.startswith("--effort="):
            raise ValueError("--effort conflicts with the workflow reasoning request.")
        if token == "--settings":
            if index + 1 >= len(tokens) or tokens[index + 1] == "--":
                raise ValueError("--settings requires a JSON object or file path.")
            _reject_claude_settings_reasoning_conflict(
                tokens[index + 1],
                working_directory,
            )
            index += 2
            continue
        if token.startswith("--settings="):
            settings_value = token.removeprefix("--settings=")
            if not settings_value:
                raise ValueError("--settings requires a JSON object or file path.")
            _reject_claude_settings_reasoning_conflict(
                settings_value,
                working_directory,
            )
        index += 1


def _reject_claude_settings_reasoning_conflict(
    settings_value: str,
    working_directory: Path | None,
) -> None:
    settings = _load_claude_settings(settings_value, working_directory)
    if _is_nonblank_settings_value(settings.get("effortLevel")):
        raise ValueError(
            "--settings effortLevel conflicts with the workflow reasoning request."
        )
    settings_environment = settings.get("env")
    if not isinstance(settings_environment, Mapping):
        return
    if _is_nonblank_settings_value(settings_environment.get(CLAUDE_REASONING_ENV)):
        raise ValueError(
            f"--settings {CLAUDE_REASONING_ENV} conflicts with the workflow "
            "reasoning request."
        )


def _load_claude_settings(
    settings_value: str,
    working_directory: Path | None,
) -> Mapping[str, object]:
    stripped_value = settings_value.lstrip()
    if stripped_value.startswith("{"):
        raw_settings = settings_value
    else:
        settings_path = Path(settings_value).expanduser()
        if not settings_path.is_absolute():
            base_directory = (
                Path.cwd() if working_directory is None else working_directory
            )
            settings_path = base_directory / settings_path
        try:
            raw_settings = settings_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(
                "Cannot validate the Claude --settings file against the workflow "
                "reasoning request."
            ) from exc
    try:
        settings: object = json.loads(raw_settings)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Claude --settings must contain a valid JSON object when workflow "
            "reasoning is requested."
        ) from exc
    if not isinstance(settings, dict):
        raise ValueError(
            "Claude --settings must contain a JSON object when workflow reasoning "
            "is requested."
        )
    return settings


def _is_nonblank_settings_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
