from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

UpdateCommand = tuple[str, ...]
CommandRunner = Callable[..., CompletedProcess[str]]
ExecutableLookup = Callable[[str], str | None]


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
