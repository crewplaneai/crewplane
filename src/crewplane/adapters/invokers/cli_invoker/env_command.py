from __future__ import annotations

__all__ = ["EnvCommandContext", "parse_env_command_context"]

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_LONG_PASSTHROUGH_OPTIONS = frozenset(
    {
        "--block-signal",
        "--debug",
        "--default-signal",
        "--ignore-signal",
        "--list-signal-handling",
        "--null",
    }
)
_SHORT_PASSTHROUGH_OPTIONS = frozenset({"0", "v"})
_SHORT_VALUE_OPTIONS = frozenset({"C", "P", "S", "a", "u"})


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
    if not tokens or Path(tokens[0]).name != "env":
        return _unwrapped_command_context(tokens, inherited_value)
    return _EnvCommandParser(
        tokens,
        inherited_value,
        tracked_environment_name,
    ).parse()


def _unwrapped_command_context(
    tokens: Sequence[str],
    inherited_value: str | None,
) -> EnvCommandContext:
    executable = tokens[0] if tokens else None
    arguments = tuple(tokens[1:]) if tokens else ()
    return EnvCommandContext(
        command_executable=executable,
        command_arguments=arguments,
        tracked_environment_value=inherited_value,
        tracked_environment_changed=False,
        command_search_path=None,
        command_working_directory=None,
        command_working_directory_option=None,
    )


class _EnvCommandParser:
    def __init__(
        self,
        tokens: Sequence[str],
        inherited_value: str | None,
        tracked_environment_name: str,
    ) -> None:
        self._tokens = tokens
        self._tracked_environment_name = tracked_environment_name
        self._tracked_environment_value = inherited_value
        self._tracked_environment_changed = False
        self._command_search_path: str | None = None
        self._command_working_directory: str | None = None
        self._command_working_directory_option: str | None = None
        self._index = 1

    def parse(self) -> EnvCommandContext:
        self._consume_options()
        self._consume_environment_reset()
        self._consume_assignments()
        return self._build_context()

    def _consume_options(self) -> None:
        while self._index < len(self._tokens):
            token = self._tokens[self._index]
            if token == "--":
                self._index += 1
                return
            if token == "-" or not token.startswith("-"):
                return
            if token.startswith("--"):
                self._consume_long_option(token)
            else:
                self._consume_short_option_cluster(token)

    def _consume_long_option(self, token: str) -> None:
        option, separator, inline_value = token.partition("=")
        value = inline_value if separator else None
        match option:
            case "--ignore-environment" if not separator:
                self._set_tracked_environment_value(None)
                self._index += 1
            case "--unset" | "--chdir" | "--argv0":
                self._consume_long_value_option(option, value)
            case "--split-string":
                raise ValueError(
                    "--split-string cannot be combined with a workflow reasoning "
                    "request."
                )
            case _ if option in _LONG_PASSTHROUGH_OPTIONS:
                self._index += 1
            case _:
                raise ValueError(
                    f"Cannot validate env option {token!r} with a workflow "
                    "reasoning request."
                )

    def _consume_long_value_option(
        self,
        option: str,
        inline_value: str | None,
    ) -> None:
        value = self._take_option_value(inline_value, option)
        if option == "--unset" and value == self._tracked_environment_name:
            self._set_tracked_environment_value(None)
        elif option == "--chdir":
            self._set_working_directory(value, option)

    def _consume_short_option_cluster(self, token: str) -> None:
        cluster = token[1:]
        for option_index, option in enumerate(cluster):
            if option == "i":
                self._set_tracked_environment_value(None)
                continue
            if option in _SHORT_PASSTHROUGH_OPTIONS:
                continue
            if option not in _SHORT_VALUE_OPTIONS:
                raise ValueError(
                    f"Cannot validate env option '-{option}' "
                    "with a workflow reasoning request."
                )
            inline_value = cluster[option_index + 1 :] or None
            self._consume_short_value_option(option, inline_value)
            return
        self._index += 1

    def _consume_short_value_option(
        self,
        option: str,
        inline_value: str | None,
    ) -> None:
        option_name = f"-{option}"
        value = self._take_option_value(inline_value, option_name)
        match option:
            case "S":
                raise ValueError(
                    f"{option_name} cannot be combined with a workflow reasoning "
                    "request."
                )
            case "u" if value == self._tracked_environment_name:
                self._set_tracked_environment_value(None)
            case "P":
                self._command_search_path = value
            case "C":
                self._set_working_directory(value, option_name)

    def _take_option_value(
        self,
        inline_value: str | None,
        option: str,
    ) -> str:
        if inline_value is not None:
            self._index += 1
            return inline_value
        value_index = self._index + 1
        if value_index >= len(self._tokens):
            raise ValueError(f"{option} requires a value.")
        self._index = value_index + 1
        return self._tokens[value_index]

    def _consume_environment_reset(self) -> None:
        if self._index >= len(self._tokens) or self._tokens[self._index] != "-":
            return
        self._set_tracked_environment_value(None)
        self._index += 1

    def _consume_assignments(self) -> None:
        while self._index < len(self._tokens):
            key, separator, value = self._tokens[self._index].partition("=")
            if not separator:
                return
            if key == self._tracked_environment_name:
                self._set_tracked_environment_value(value)
            self._index += 1

    def _set_tracked_environment_value(self, value: str | None) -> None:
        self._tracked_environment_value = value
        self._tracked_environment_changed = True

    def _set_working_directory(self, value: str, option: str) -> None:
        self._command_working_directory = value
        self._command_working_directory_option = option

    def _build_context(self) -> EnvCommandContext:
        command = self._tokens[self._index :]
        executable = command[0] if command else None
        arguments = tuple(command[1:])
        return EnvCommandContext(
            command_executable=executable,
            command_arguments=arguments,
            tracked_environment_value=self._tracked_environment_value,
            tracked_environment_changed=self._tracked_environment_changed,
            command_search_path=self._command_search_path,
            command_working_directory=self._command_working_directory,
            command_working_directory_option=self._command_working_directory_option,
        )
