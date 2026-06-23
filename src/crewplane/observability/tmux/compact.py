from __future__ import annotations

import sys
from collections.abc import Callable
from threading import Event, Thread

from crewplane.observability.events import ExecutionEvent
from crewplane.observability.tmux.bindings import TmuxCompactKeyBindings
from crewplane.observability.tmux.client import (
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
)
from crewplane.observability.tmux.commands import build_attach_command
from crewplane.observability.tmux.control_state import TmuxCompactControlState
from crewplane.observability.tmux.refresh import (
    RefreshOutcome,
    TmuxCompactRefreshController,
)
from crewplane.observability.tmux.session_lifecycle import (
    CompactSessionLifecycle,
    StartedCompactSession,
    TmuxClientFactory,
    TmuxCompactSessionLifecycle,
)
from crewplane.observability.tmux.window import TmuxCompactWindowOptions
from crewplane.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)

DEFAULT_QUIET_AFTER_SECONDS = 120.0

__all__ = [
    "DEFAULT_QUIET_AFTER_SECONDS",
    "DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS",
    "TmuxCompactRuntime",
    "build_attach_command",
]


class TmuxCompactRuntime:
    """Render a compact tmux dashboard for a running workflow."""

    def __init__(
        self,
        auto_close_session: bool = True,
        tmux_executable: str = "tmux",
        warning_sink: Callable[[str], None] | None = None,
        refresh_interval_seconds: float = 0.25,
        log_tail_lines: int | None = None,
        quiet_after_seconds: float = DEFAULT_QUIET_AFTER_SECONDS,
        tmux_command_timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
        lifecycle: CompactSessionLifecycle | None = None,
        window: TmuxCompactWindowOptions | None = None,
        bindings: TmuxCompactKeyBindings | None = None,
        control_state: TmuxCompactControlState | None = None,
        refresh_controller: TmuxCompactRefreshController | None = None,
        tmux_client_factory: TmuxClientFactory | None = None,
        monotonic_now: Callable[[], float] | None = None,
        wall_time_now: Callable[[], float] | None = None,
    ) -> None:
        self._auto_close_session = auto_close_session
        self._warning_sink = warning_sink
        self._refresh_interval_seconds = max(0.1, refresh_interval_seconds)

        self._window = window or TmuxCompactWindowOptions()
        self._bindings = bindings or TmuxCompactKeyBindings(
            tmux_executable=tmux_executable,
            refresh_interval_seconds=self._refresh_interval_seconds,
        )
        self._control_state = control_state or TmuxCompactControlState()
        self._refresh = refresh_controller or TmuxCompactRefreshController(
            window=self._window,
            bindings=self._bindings,
            control_state=self._control_state,
            log_tail_lines=log_tail_lines,
            quiet_after_seconds=quiet_after_seconds,
            **_clock_kwargs(monotonic_now, wall_time_now),
        )
        self._lifecycle = lifecycle or TmuxCompactSessionLifecycle(
            auto_close_session=auto_close_session,
            tmux_executable=tmux_executable,
            refresh_interval_seconds=self._refresh_interval_seconds,
            warning_sink=warning_sink,
            tmux_command_timeout_seconds=tmux_command_timeout_seconds,
            client_factory=tmux_client_factory,
        )

        self._session: StartedCompactSession | None = None
        self._stop_event = Event()
        self._stop_requested = Event()
        self._refresh_thread: Thread | None = None

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def start(self, context: RunContext) -> None:
        self._reset_for_start()
        session: StartedCompactSession | None = None
        try:
            session = self._lifecycle.create_session(context)
            self._session = session
            self._window.configure(session.tmux, session.targets)
            self._bindings.install(session.tmux, session.runtime_files, session.targets)
            self._lifecycle.attach_or_switch(session)
            self._start_refresh_thread()
        except Exception:
            self._lifecycle.rollback_start(session)
            self._session = None
            raise

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        self._refresh.on_snapshot(event, snapshot)

    def refresh_once(self) -> RefreshOutcome:
        session = self._session
        if session is None:
            return RefreshOutcome()
        outcome = self._refresh.refresh_once(session)
        if outcome.stop_reason is not None:
            self._request_stop()
        return outcome

    def stop(self, result: RunResult) -> None:
        if not isinstance(result, RunResult):
            raise TypeError("result must be a RunResult instance")
        self._stop_refresh_thread()
        self._render_terminal_result(result)
        try:
            self._lifecycle.stop_session(self._session, self._auto_close_session)
        finally:
            if self._auto_close_session:
                self._session = None
                self._control_state.reset()

    def _render_terminal_result(self, result: RunResult) -> None:
        if self._session is None or self._auto_close_session:
            return
        try:
            self._refresh.render_terminal_result(self._session, result)
        except Exception as exc:  # pragma: no cover - defensive shutdown path
            self._warn(f"tmux compact final render failed: {exc}")

    def _reset_for_start(self) -> None:
        self._stop_requested.clear()
        self._stop_event.clear()
        self._window.reset()
        self._bindings.reset()
        self._control_state.reset()
        self._refresh.reset()

    def _start_refresh_thread(self) -> None:
        self._stop_event.clear()
        self._refresh_thread = Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    def _stop_refresh_thread(self) -> None:
        self._stop_event.set()
        if self._refresh_thread is None:
            return
        self._refresh_thread.join(timeout=1.0)
        self._refresh_thread = None

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(self._refresh_interval_seconds):
            try:
                self.refresh_once()
            except Exception as exc:  # pragma: no cover - defensive
                self._warn(f"tmux compact refresh failed: {exc}")

    def _request_stop(self) -> None:
        self._stop_requested.set()
        self._stop_event.set()

    def _warn(self, message: str) -> None:
        if self._warning_sink is not None:
            try:
                self._warning_sink(message)
            except Exception:
                return
            return
        print(f"WARN: {message}", file=sys.stderr)


def _clock_kwargs(
    monotonic_now: Callable[[], float] | None,
    wall_time_now: Callable[[], float] | None,
) -> dict[str, Callable[[], float]]:
    kwargs: dict[str, Callable[[], float]] = {}
    if monotonic_now is not None:
        kwargs["monotonic_now"] = monotonic_now
    if wall_time_now is not None:
        kwargs["wall_time_now"] = wall_time_now
    return kwargs
