from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from crewplane.core.config import Config
from crewplane.core.platform import is_native_windows
from crewplane.core.workflow.models import WorkflowPlan

from .command_types import CommandAvailability

if TYPE_CHECKING:
    from .capability import CliInvocationRequest


class CommandStrategy(Protocol):
    def collect_availability_errors(
        self,
        workflow: WorkflowPlan,
        config: Config,
        lookup: Callable[[str], str | None],
        root: Path,
    ) -> list[str]: ...
    def availability(
        self, command: list[str], root: Path, lookup: Callable[[str], str | None]
    ) -> CommandAvailability: ...
    def validate_request(self, request: CliInvocationRequest) -> None: ...
    def prepare_executable(
        self, executable: str, request: CliInvocationRequest
    ) -> str: ...
    def prepare_arguments(
        self, command: list[str], request: CliInvocationRequest
    ) -> list[str]: ...


def command_strategy() -> CommandStrategy:
    if is_native_windows():
        from .command_windows import WindowsCommandStrategy

        return WindowsCommandStrategy()
    from .command_posix import PosixCommandStrategy

    return PosixCommandStrategy()
