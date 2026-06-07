from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from time import monotonic, time

from orchestrator_cli.observability.events import ExecutionEvent, NodeRuntimeState
from orchestrator_cli.observability.tmux.bindings import TmuxCompactKeyBindings
from orchestrator_cli.observability.tmux.control_state import (
    PaneGeometry,
    TmuxCompactControlState,
)
from orchestrator_cli.observability.tmux.labels import counts_line, status_line
from orchestrator_cli.observability.tmux.rendering import (
    DashboardSelection,
    SelectedOutputRenderContext,
    render_left_dashboard,
    render_selected_output,
    resolve_dashboard_selection,
    right_pane_title,
    selected_invocation_log_path,
)
from orchestrator_cli.observability.tmux.runtime_files import (
    MODE_INSPECT,
    RuntimeFiles,
    read_index,
    read_runtime_mode,
    read_runtime_value,
    write_atomic,
)
from orchestrator_cli.observability.tmux.selected_invocation import (
    prepare_selected_invocation,
)
from orchestrator_cli.observability.tmux.session_lifecycle import StartedCompactSession
from orchestrator_cli.observability.tmux.window import TmuxCompactWindowOptions
from orchestrator_cli.observability.types import DashboardSnapshot


class StopReason(StrEnum):
    QUIT_REQUESTED = "quit_requested"
    SESSION_GONE = "session_gone"


@dataclass(frozen=True)
class RefreshOutcome:
    stop_reason: StopReason | None = None


class TmuxCompactRefreshController:
    """Own compact dashboard snapshot storage and refresh rendering."""

    def __init__(
        self,
        window: TmuxCompactWindowOptions,
        bindings: TmuxCompactKeyBindings,
        control_state: TmuxCompactControlState,
        log_tail_lines: int | None = None,
        quiet_after_seconds: float = 120.0,
        monotonic_now: Callable[[], float] = monotonic,
        wall_time_now: Callable[[], float] = time,
    ) -> None:
        self._window = window
        self._bindings = bindings
        self._control_state = control_state
        self._log_tail_lines = log_tail_lines
        self._quiet_after_seconds = quiet_after_seconds
        self._monotonic_now = monotonic_now
        self._wall_time_now = wall_time_now
        self._snapshot_lock = Lock()
        self._latest_snapshot: DashboardSnapshot | None = None

    def reset(self) -> None:
        with self._snapshot_lock:
            self._latest_snapshot = None

    def on_snapshot(
        self,
        event: ExecutionEvent | None,  # noqa: ARG002 - Protocol callback signature.
        snapshot: DashboardSnapshot,
    ) -> None:
        with self._snapshot_lock:
            self._latest_snapshot = copy.deepcopy(snapshot)

    def refresh_once(self, session: StartedCompactSession) -> RefreshOutcome:
        runtime_files = session.runtime_files
        targets = session.targets
        tmux = session.tmux
        if self._quit_requested(runtime_files):
            return RefreshOutcome(stop_reason=StopReason.QUIT_REQUESTED)
        if not tmux.session_exists(targets.session_name):
            return RefreshOutcome(stop_reason=StopReason.SESSION_GONE)

        with self._snapshot_lock:
            snapshot = self._latest_snapshot
        if snapshot is None:
            return RefreshOutcome()

        state = snapshot.state
        selection = resolve_dashboard_selection(
            snapshot,
            read_index(runtime_files.selection_index),
        )
        selected_node_id = selection.selected_node_id
        self._write_runtime_selection(runtime_files, selection, state.nodes)

        mode = read_runtime_mode(runtime_files.mode)
        inspect_node_id = read_runtime_value(runtime_files.inspect_node_id)
        inspect_mode = mode == MODE_INSPECT
        self._bindings.sync_copy_mode_bindings(
            tmux,
            runtime_files,
            targets,
            mode,
        )

        pane_geometry, timed_out = self._pane_geometry(session)
        if not timed_out:
            self._control_state.recover_after_resize(
                tmux,
                mode,
                targets,
                pane_geometry,
            )

        left_lines = render_left_dashboard(
            snapshot=snapshot,
            selected_node_id=selected_node_id,
            width=pane_geometry.left_width,
            height=pane_geometry.left_height,
            inspect_mode=inspect_mode,
            now=self._monotonic_now(),
        )
        write_atomic(runtime_files.left_content, "\n".join(left_lines))

        if not inspect_mode:
            self.render_selected_output(
                runtime_files,
                state.nodes,
                selected_node_id,
                pane_geometry,
            )

        self._window.set_status_option_if_changed(
            tmux,
            targets.session_name,
            "status-left",
            status_line(state),
        )
        self._window.set_status_option_if_changed(
            tmux,
            targets.session_name,
            "status-right",
            counts_line(state),
        )
        self._window.set_pane_title_if_changed(
            tmux,
            targets.right_pane_id,
            right_pane_title(
                mode=mode,
                selected_node_id=selected_node_id,
                inspect_node_id=inspect_node_id,
            ),
        )
        return RefreshOutcome()

    def render_selected_output(
        self,
        runtime_files: RuntimeFiles,
        nodes: Mapping[str, NodeRuntimeState],
        selected_node_id: str | None,
        pane_geometry: PaneGeometry,
    ) -> None:
        prepared_invocation = prepare_selected_invocation(
            nodes=nodes,
            selected_node_id=selected_node_id,
            pane_height=pane_geometry.right_height,
            log_tail_lines=self._log_tail_lines,
            wall_time_now=self._wall_time_now(),
        )
        right_lines = render_selected_output(
            SelectedOutputRenderContext(
                nodes=nodes,
                selected_node_id=selected_node_id,
                width=pane_geometry.right_width,
                pane_height=pane_geometry.right_height,
                log_tail_lines=self._log_tail_lines,
                quiet_after_seconds=self._quiet_after_seconds,
                monotonic_now=self._monotonic_now(),
                prepared_invocation=prepared_invocation,
            )
        )
        write_atomic(runtime_files.right_content, "\n".join(right_lines))

    def _pane_geometry(
        self,
        session: StartedCompactSession,
    ) -> tuple[PaneGeometry, bool]:
        targets = session.targets
        tmux = session.tmux
        left_width, left_width_timed_out = tmux.pane_dimension(
            targets.left_pane_id,
            "#{pane_width}",
            80,
        )
        left_height, left_height_timed_out = tmux.pane_dimension(
            targets.left_pane_id,
            "#{pane_height}",
            24,
        )
        right_width, right_width_timed_out = tmux.pane_dimension(
            targets.right_pane_id,
            "#{pane_width}",
            100,
        )
        right_height, right_height_timed_out = tmux.pane_dimension(
            targets.right_pane_id,
            "#{pane_height}",
            24,
        )
        return (
            PaneGeometry(
                left_width=left_width,
                left_height=left_height,
                right_width=right_width,
                right_height=right_height,
            ),
            any(
                (
                    left_width_timed_out,
                    left_height_timed_out,
                    right_width_timed_out,
                    right_height_timed_out,
                )
            ),
        )

    def _write_runtime_selection(
        self,
        runtime_files: RuntimeFiles,
        selection: DashboardSelection,
        nodes: Mapping[str, NodeRuntimeState],
    ) -> None:
        write_atomic(runtime_files.node_count, str(len(selection.ordered_node_ids)))
        write_atomic(runtime_files.selection_index, str(selection.selected_index))
        write_atomic(runtime_files.selected_node_id, selection.selected_node_id or "")
        write_atomic(
            runtime_files.selected_log,
            selected_invocation_log_path(nodes, selection.selected_node_id) or "",
        )

    def _quit_requested(self, runtime_files: RuntimeFiles) -> bool:
        return read_runtime_value(runtime_files.quit_requested) == "1"
