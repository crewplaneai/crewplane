from __future__ import annotations

__all__ = ["EnvCommandContext", "parse_env_command_context"]

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EnvCommandContext:
    """Effective command arguments and tracked value after an env prefix."""

    command_arguments: tuple[str, ...]
    tracked_environment_value: str


def parse_env_command_context(
    tokens: Sequence[str],
    inherited_value: str,
    tracked_environment_name: str,
) -> EnvCommandContext:
    """Interpret supported env options without mutating the supplied tokens."""
    if not tokens:
        return EnvCommandContext((), inherited_value)
    if Path(tokens[0]).name != "env":
        return EnvCommandContext(tuple(tokens[1:]), inherited_value)

    effective_value = inherited_value
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token == "-":
            effective_value = ""
            index += 1
            continue
        if token.startswith("--"):
            index, effective_value = _consume_long_option(
                tokens,
                index,
                effective_value,
                tracked_environment_name,
            )
            continue
        if token.startswith("-"):
            index, effective_value = _consume_short_options(
                tokens,
                index,
                effective_value,
                tracked_environment_name,
            )
            continue
        break

    while index < len(tokens):
        key, separator, value = tokens[index].partition("=")
        if not separator:
            break
        if key == tracked_environment_name:
            effective_value = value
        index += 1
    command_arguments = tuple(tokens[index + 1 :]) if index < len(tokens) else ()
    return EnvCommandContext(command_arguments, effective_value)


def _consume_long_option(
    tokens: Sequence[str],
    index: int,
    effective_value: str,
    tracked_environment_name: str,
) -> tuple[int, str]:
    token = tokens[index]
    option, separator, inline_value = token.partition("=")
    if option == "--ignore-environment" and not separator:
        return index + 1, ""
    if option == "--unset":
        value, next_index = _option_value(
            tokens,
            index,
            inline_value if separator else None,
            option,
        )
        if value == tracked_environment_name:
            effective_value = ""
        return next_index, effective_value
    if option == "--chdir":
        raise ValueError(
            "--chdir cannot be combined with a workflow reasoning request."
        )
    if option == "--argv0":
        _, next_index = _option_value(
            tokens,
            index,
            inline_value if separator else None,
            option,
        )
        return next_index, effective_value
    if option == "--split-string":
        raise ValueError(
            "--split-string cannot be combined with a workflow reasoning request."
        )
    if option in {
        "--block-signal",
        "--debug",
        "--default-signal",
        "--ignore-signal",
        "--list-signal-handling",
        "--null",
    }:
        return index + 1, effective_value
    raise ValueError(
        f"Cannot validate env option {token!r} with a workflow reasoning request."
    )


def _consume_short_options(
    tokens: Sequence[str],
    index: int,
    effective_value: str,
    tracked_environment_name: str,
) -> tuple[int, str]:
    cluster = tokens[index][1:]
    option_index = 0
    while option_index < len(cluster):
        option = cluster[option_index]
        if option == "i":
            effective_value = ""
            option_index += 1
            continue
        if option in {"0", "v"}:
            option_index += 1
            continue
        if option not in {"C", "P", "S", "a", "u"}:
            raise ValueError(
                f"Cannot validate env option '-{option}' "
                "with a workflow reasoning request."
            )
        inline_value = cluster[option_index + 1 :] or None
        value, next_index = _option_value(
            tokens,
            index,
            inline_value,
            f"-{option}",
        )
        if option in {"C", "S"}:
            raise ValueError(
                f"-{option} cannot be combined with a workflow reasoning request."
            )
        if option == "u" and value == tracked_environment_name:
            effective_value = ""
        return next_index, effective_value
    return index + 1, effective_value


def _option_value(
    tokens: Sequence[str],
    index: int,
    inline_value: str | None,
    option: str,
) -> tuple[str, int]:
    if inline_value is not None:
        return inline_value, index + 1
    if index + 1 >= len(tokens):
        raise ValueError(f"{option} requires a value.")
    return tokens[index + 1], index + 2
