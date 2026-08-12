from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal, Protocol, overload

UpdateCommand = tuple[str, ...]
ExecutableLookup = Callable[[str], str | None]


class CommandRunner(Protocol):
    """Run update commands in either inherited-stream or captured-text mode."""

    @overload
    def __call__(
        self,
        args: Sequence[str],
        capture_output: Literal[True],
        text: Literal[True],
        check: Literal[False],
        timeout: float,
        shell: Literal[False],
    ) -> CompletedProcess[str]: ...

    @overload
    def __call__(
        self,
        args: Sequence[str],
        check: Literal[False],
        shell: Literal[False],
    ) -> CompletedProcess[bytes]: ...


class UpdateError(RuntimeError):
    """Raised when Crewplane cannot safely update the active installation."""


@dataclass(frozen=True)
class InstalledMetadata:
    installer: str | None
    editable: bool = False
    direct_source: bool = False


@dataclass(frozen=True)
class UpdatePlan:
    owner: str
    command: UpdateCommand
    verification_command: UpdateCommand


@dataclass(frozen=True)
class UpdateContext:
    package_name: str
    python_executable: Path
    environment_root: Path
    metadata: InstalledMetadata
    current_version: str
    executable_lookup: ExecutableLookup
    command_runner: CommandRunner
