from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from typing import Protocol

DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS = 1.0
TMUX_TIMEOUT_RETURN_CODE = 124
TMUX_TIMEOUT_STDERR = "tmux command timed out"


class TmuxSessionClient(Protocol):
    @property
    def socket_name(self) -> str | None: ...

    def run(
        self,
        args: list[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...

    def pane_dimension(
        self,
        pane_id: str,
        format_string: str,
        default: int,
    ) -> tuple[int, bool]: ...

    def session_exists(self, session_name: str) -> bool: ...


class TmuxCommandClient:
    """Run tmux commands for one compact runtime socket."""

    def __init__(
        self,
        tmux_executable: str = "tmux",
        socket_name: str | None = None,
        warning_sink: Callable[[str], None] | None = None,
        timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._tmux_executable = tmux_executable
        self._socket_name = socket_name
        self._warning_sink = warning_sink
        self._timeout_seconds = max(0.01, timeout_seconds)
        self._timeout_warning_emitted = False

    @property
    def socket_name(self) -> str | None:
        return self._socket_name

    def run(
        self,
        args: list[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = self._command(args)
        try:
            return subprocess.run(
                command,
                check=check,
                capture_output=capture_output,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self._warn_tmux_timeout(command)
            if check:
                raise RuntimeError(
                    "tmux command timed out after "
                    f"{self._timeout_seconds:.2f}s: {' '.join(command)}"
                ) from exc
            return subprocess.CompletedProcess(
                command,
                TMUX_TIMEOUT_RETURN_CODE,
                stdout="",
                stderr=(f"{TMUX_TIMEOUT_STDERR} after {self._timeout_seconds:.2f}s"),
            )

    def pane_dimension(
        self,
        pane_id: str,
        format_string: str,
        default: int,
    ) -> tuple[int, bool]:
        result = self.run(
            ["display-message", "-p", "-t", pane_id, format_string],
            capture_output=True,
            check=False,
        )
        if tmux_result_timed_out(result):
            return default, True
        if result.returncode != 0:
            return default, False
        try:
            return max(1, int(result.stdout.strip())), False
        except ValueError:
            return default, False

    def session_exists(self, session_name: str) -> bool:
        result = self.run(
            ["has-session", "-t", session_name],
            capture_output=True,
            check=False,
        )
        if tmux_result_timed_out(result):
            return True
        return result.returncode == 0

    def _command(self, args: list[str]) -> list[str]:
        command = [self._tmux_executable]
        if self._socket_name:
            command.extend(["-L", self._socket_name])
        command.extend(args)
        return command

    def _warn_tmux_timeout(self, command: list[str]) -> None:
        if self._timeout_warning_emitted:
            return
        self._timeout_warning_emitted = True
        self._warn(
            f"tmux command timed out; live dashboard may be stale: {' '.join(command)}"
        )

    def _warn(self, message: str) -> None:
        if self._warning_sink is not None:
            try:
                self._warning_sink(message)
            except Exception:
                return
            return
        print(f"WARN: {message}", file=sys.stderr)


def tmux_result_timed_out(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == TMUX_TIMEOUT_RETURN_CODE and (
        TMUX_TIMEOUT_STDERR in (result.stderr or "")
    )
