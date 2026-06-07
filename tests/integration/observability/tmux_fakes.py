from __future__ import annotations

import subprocess
from collections.abc import Callable
from time import monotonic, time

from orchestrator_cli.observability.tmux.client import tmux_result_timed_out
from orchestrator_cli.observability.tmux.compact import TmuxCompactRuntime
from orchestrator_cli.observability.tmux.refresh import RefreshOutcome
from orchestrator_cli.observability.tmux.runtime_files import (
    RuntimeFiles,
    write_atomic,
)
from orchestrator_cli.observability.tmux.session_lifecycle import (
    StartedCompactSession,
    TmuxCompactSessionLifecycle,
)
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)


class FakeTmuxClient:
    def __init__(self) -> None:
        self._socket_name: str | None = None
        self.calls: list[tuple[list[str], bool, bool]] = []
        self.call_sockets: list[str | None] = []
        self.left_pane_width = 72
        self.right_pane_width = 96
        self.left_pane_height = 18
        self.right_pane_height = 24
        self._next_pane_id = 20
        self.session_exists_value = True
        self.has_session_times_out = False
        self.display_message_times_out = False
        self.fail_next_key_table_restore = False
        self.fail_kill_session = False
        self.fail_status_option = False
        self.fail_pane_title = False

    @property
    def socket_name(self) -> str | None:
        return self._socket_name

    def set_socket_name(self, socket_name: str | None) -> None:
        self._socket_name = socket_name

    def run(
        self,
        args: list[str],
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, capture_output, check))
        self.call_sockets.append(self.socket_name)
        command = args[0]
        if command == "kill-session" and self.fail_kill_session:
            raise subprocess.SubprocessError("rollback failed")
        if command == "new-session" and capture_output:
            return subprocess.CompletedProcess(
                ["tmux", *args], 0, stdout="%10\n", stderr=""
            )
        if command == "split-window" and capture_output:
            pane_id = f"%{self._next_pane_id}"
            self._next_pane_id += 1
            return subprocess.CompletedProcess(
                ["tmux", *args], 0, stdout=f"{pane_id}\n", stderr=""
            )
        if command == "has-session":
            if self.has_session_times_out:
                return _timeout_result(args)
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0 if self.session_exists_value else 1,
                stdout="",
                stderr="",
            )
        if self._fails_key_table_restore(args):
            self.fail_next_key_table_restore = False
            return subprocess.CompletedProcess(
                ["tmux", *args],
                1,
                stdout="",
                stderr="transient key-table failure",
            )
        if self._fails_status_option(args) or self._fails_pane_title(args):
            return subprocess.CompletedProcess(
                ["tmux", *args],
                1,
                stdout="",
                stderr="option write failed",
            )
        if self._is_dimension_query(command, args, capture_output):
            if self.display_message_times_out:
                return _timeout_result(args)
            target = args[args.index("-t") + 1]
            value = self._dimension_value(target, args[-1])
            return subprocess.CompletedProcess(
                ["tmux", *args],
                0,
                stdout=f"{value}\n",
                stderr="",
            )
        return subprocess.CompletedProcess(["tmux", *args], 0, stdout="", stderr="")

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

    def _fails_key_table_restore(self, args: list[str]) -> bool:
        return (
            args[0] == "set-option"
            and len(args) >= 5
            and args[3] == "key-table"
            and self.fail_next_key_table_restore
        )

    def _fails_status_option(self, args: list[str]) -> bool:
        return (
            self.fail_status_option
            and len(args) >= 5
            and args[0] == "set-option"
            and args[3] in {"status-left", "status-right"}
        )

    def _fails_pane_title(self, args: list[str]) -> bool:
        return (
            self.fail_pane_title
            and len(args) >= 6
            and args[:3] == ["set-option", "-p", "-t"]
            and args[4] == "@orchestrator_title"
        )

    def _is_dimension_query(
        self,
        command: str,
        args: list[str],
        capture_output: bool,
    ) -> bool:
        return (
            command == "display-message"
            and capture_output
            and args[-1] in {"#{pane_width}", "#{pane_height}"}
        )

    def _dimension_value(self, pane_id: str, format_string: str) -> int:
        if format_string == "#{pane_width}":
            return self.left_pane_width if pane_id == "%10" else self.right_pane_width
        return self.left_pane_height if pane_id == "%10" else self.right_pane_height


class FakeCompactSessionLifecycle:
    def __init__(
        self,
        auto_close_session: bool,
        client: FakeTmuxClient,
        tmux_executable: str = "tmux",
        warning_sink: Callable[[str], None] | None = None,
        refresh_interval_seconds: float = 1000.0,
    ) -> None:
        self.session: StartedCompactSession | None = None
        self.last_session: StartedCompactSession | None = None
        self.attach_failure: Exception | None = None
        self.create_failure: Exception | None = None
        self.rollback_count = 0
        self.stop_count = 0
        self._client = client
        self._real = TmuxCompactSessionLifecycle(
            auto_close_session=auto_close_session,
            tmux_executable=tmux_executable,
            refresh_interval_seconds=refresh_interval_seconds,
            warning_sink=warning_sink,
            client_factory=self._client_factory,
        )

    def create_session(self, context: RunContext) -> StartedCompactSession:
        if self.create_failure is not None:
            raise self.create_failure
        self.session = self._real.create_session(context)
        self.last_session = self.session
        return self.session

    def attach_or_switch(self, session: StartedCompactSession) -> None:
        session.attach_attempted = True
        if self.attach_failure is not None:
            raise self.attach_failure

    def rollback_start(self, session: StartedCompactSession | None) -> None:
        self.rollback_count += 1
        self._real.rollback_start(session)
        if session is self.session:
            self.session = None

    def stop_session(
        self,
        session: StartedCompactSession | None,
        auto_close_session: bool,
    ) -> None:
        self.stop_count += 1
        self._real.stop_session(session, auto_close_session)
        if auto_close_session and session is self.session:
            self.session = None

    def _client_factory(self, socket_name: str | None) -> FakeTmuxClient:
        self._client.set_socket_name(socket_name)
        return self._client


class SimulatedTmuxRuntime:
    def __init__(
        self,
        auto_close_session: bool = False,
        quiet_after_seconds: float = 120.0,
        log_tail_lines: int | None = None,
        warning_sink: Callable[[str], None] | None = None,
        tmux_executable: str = "tmux",
    ) -> None:
        self.client = FakeTmuxClient()
        self.lifecycle = FakeCompactSessionLifecycle(
            auto_close_session=auto_close_session,
            client=self.client,
            tmux_executable=tmux_executable,
            warning_sink=warning_sink,
        )
        self.monotonic_now_override: float | None = None
        self.wall_time_now_override: float | None = None
        self.runtime = TmuxCompactRuntime(
            auto_close_session=auto_close_session,
            tmux_executable=tmux_executable,
            warning_sink=warning_sink,
            refresh_interval_seconds=1000.0,
            log_tail_lines=log_tail_lines,
            quiet_after_seconds=quiet_after_seconds,
            lifecycle=self.lifecycle,
            monotonic_now=self._monotonic_now,
            wall_time_now=self._wall_time_now,
        )

    @property
    def calls(self) -> list[tuple[list[str], bool, bool]]:
        return self.client.calls

    @property
    def call_sockets(self) -> list[str | None]:
        return self.client.call_sockets

    @property
    def session(self) -> StartedCompactSession:
        session = self.lifecycle.session or self.lifecycle.last_session
        if session is None:
            raise RuntimeError("simulated tmux runtime has no session")
        return session

    @property
    def runtime_files(self) -> RuntimeFiles:
        return self.session.runtime_files

    @property
    def stop_requested(self) -> bool:
        return self.runtime.stop_requested

    @property
    def left_pane_width(self) -> int:
        return self.client.left_pane_width

    @left_pane_width.setter
    def left_pane_width(self, value: int) -> None:
        self.client.left_pane_width = value

    @property
    def right_pane_width(self) -> int:
        return self.client.right_pane_width

    @right_pane_width.setter
    def right_pane_width(self, value: int) -> None:
        self.client.right_pane_width = value

    @property
    def left_pane_height(self) -> int:
        return self.client.left_pane_height

    @left_pane_height.setter
    def left_pane_height(self, value: int) -> None:
        self.client.left_pane_height = value

    @property
    def right_pane_height(self) -> int:
        return self.client.right_pane_height

    @right_pane_height.setter
    def right_pane_height(self, value: int) -> None:
        self.client.right_pane_height = value

    def start(self, context: RunContext) -> None:
        self.runtime.start(context)

    def on_snapshot(
        self,
        event: object | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        self.runtime.on_snapshot(event, snapshot)  # type: ignore[arg-type]

    def refresh_once(self) -> RefreshOutcome:
        return self.runtime.refresh_once()

    def stop(self, result: RunResult) -> None:
        self.runtime.stop(result)

    def write_runtime_file(self, path: str, content: str) -> None:
        write_atomic(getattr(self.runtime_files, path), content)

    def cleanup_preserved_runtime(self) -> None:
        self.session.runtime_lease.cleanup(force=True)

    def _monotonic_now(self) -> float:
        if self.monotonic_now_override is not None:
            return self.monotonic_now_override
        return monotonic()

    def _wall_time_now(self) -> float:
        if self.wall_time_now_override is not None:
            return self.wall_time_now_override
        return time()


def _timeout_result(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["tmux", *args],
        124,
        stdout="",
        stderr="tmux command timed out after 1.00s",
    )
