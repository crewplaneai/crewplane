from __future__ import annotations

__all__ = ["EnvCommandContext", "parse_env_command_context"]

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EnvCommandContext:
    """Effective command arguments and tracked value after an env prefix."""

    command_executable: str | None
    command_arguments: tuple[str, ...]
    tracked_environment_value: str | None
    tracked_environment_changed: bool
    command_search_path: str | None
    command_working_directory: str | None
    command_working_directory_option: str | None


def parse_env_command_context(
    tokens: Sequence[str],
    inherited_value: str | None,
    tracked_environment_name: str,
) -> EnvCommandContext:
    """Interpret supported env options without mutating the supplied tokens."""
    if not tokens:
        return EnvCommandContext(None, (), inherited_value, False, None, None, None)
    if Path(tokens[0]).name != "env":
        return EnvCommandContext(
            tokens[0], tuple(tokens[1:]), inherited_value, False, None, None, None
        )

    effective_value = inherited_value
    tracked_environment_changed = False
    command_search_path = None
    command_working_directory = None
    command_working_directory_option = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token == "-":
            break
        if token.startswith("--"):
            (
                index,
                effective_value,
                changed,
                path_override,
                working_directory,
                working_directory_option,
            ) = _consume_long_option(
                tokens, index, effective_value, tracked_environment_name
            )
            tracked_environment_changed |= changed
            if path_override is not None:
                command_search_path = path_override
            if working_directory is not None:
                command_working_directory = working_directory
                command_working_directory_option = working_directory_option
            continue
        if token.startswith("-"):
            (
                index,
                effective_value,
                changed,
                path_override,
                working_directory,
                working_directory_option,
            ) = _consume_short_options(
                tokens, index, effective_value, tracked_environment_name
            )
            tracked_environment_changed |= changed
            if path_override is not None:
                command_search_path = path_override
            if working_directory is not None:
                command_working_directory = working_directory
                command_working_directory_option = working_directory_option
            continue
        break

    if index < len(tokens) and tokens[index] == "-":
        effective_value = None
        tracked_environment_changed = True
        index += 1

    while index < len(tokens):
        key, separator, value = tokens[index].partition("=")
        if not separator:
            break
        if key == tracked_environment_name:
            effective_value = value
            tracked_environment_changed = True
        index += 1
    command_executable = tokens[index] if index < len(tokens) else None
    command_arguments = tuple(tokens[index + 1 :]) if index < len(tokens) else ()
    return EnvCommandContext(
        command_executable,
        command_arguments,
        effective_value,
        tracked_environment_changed,
        command_search_path,
        command_working_directory,
        command_working_directory_option,
    )


def _consume_long_option(
    tokens: Sequence[str],
    index: int,
    effective_value: str | None,
    tracked_environment_name: str,
) -> tuple[int, str | None, bool, str | None, str | None, str | None]:
    token = tokens[index]
    option, separator, inline_value = token.partition("=")
    if option == "--ignore-environment" and not separator:
        return index + 1, None, True, None, None, None
    if option == "--unset":
        value, next_index = _option_value(
            tokens,
            index,
            inline_value if separator else None,
            option,
        )
        if value == tracked_environment_name:
            return next_index, None, True, None, None, None
        return next_index, effective_value, False, None, None, None
    if option == "--chdir":
        value, next_index = _option_value(
            tokens,
            index,
            inline_value if separator else None,
            option,
        )
        return next_index, effective_value, False, None, value, option
    if option == "--argv0":
        _, next_index = _option_value(
            tokens,
            index,
            inline_value if separator else None,
            option,
        )
        return next_index, effective_value, False, None, None, None
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
        return index + 1, effective_value, False, None, None, None
    raise ValueError(
        f"Cannot validate env option {token!r} with a workflow reasoning request."
    )


def _consume_short_options(
    tokens: Sequence[str],
    index: int,
    effective_value: str | None,
    tracked_environment_name: str,
) -> tuple[int, str | None, bool, str | None, str | None, str | None]:
    cluster = tokens[index][1:]
    option_index = 0
    tracked_environment_changed = False
    while option_index < len(cluster):
        option = cluster[option_index]
        if option == "i":
            effective_value = None
            tracked_environment_changed = True
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
        if option == "S":
            raise ValueError(
                f"-{option} cannot be combined with a workflow reasoning request."
            )
        if option == "u" and value == tracked_environment_name:
            effective_value = None
            tracked_environment_changed = True
        path_override = value if option == "P" else None
        working_directory = value if option == "C" else None
        working_directory_option = f"-{option}" if option == "C" else None
        return (
            next_index,
            effective_value,
            tracked_environment_changed,
            path_override,
            working_directory,
            working_directory_option,
        )
    return index + 1, effective_value, tracked_environment_changed, None, None, None


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
