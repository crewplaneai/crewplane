from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from time import monotonic, time
from typing import Any

from crewplane.observability.events import (
    ExecutionEvent,
    NodeRuntimeState,
    RunDashboardState,
)
from crewplane.observability.events.dashboard_state import (
    InvocationRuntimeState,
)
from crewplane.observability.log_presentation import JSON_OBJECT_THROTTLE
from crewplane.observability.tmux.bindings import TmuxCompactKeyBindings
from crewplane.observability.tmux.control_state import (
    PaneGeometry,
    TmuxCompactControlState,
)
from crewplane.observability.tmux.inspect_snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    read_snapshot,
)
from crewplane.observability.tmux.labels import counts_line, status_line
from crewplane.observability.tmux.rendering import (
    SelectedOutputRenderContext,
    render_left_dashboard,
    render_selected_output,
    right_pane_title,
)
from crewplane.observability.tmux.runtime_files import (
    MODE_INSPECT,
    RuntimeFiles,
    read_runtime_mode,
    read_runtime_value,
    write_atomic,
    write_json_atomic,
)
from crewplane.observability.tmux.selected_invocation import (
    prepare_selected_invocation,
)
from crewplane.observability.tmux.selection import (
    DashboardSelection,
    resolve_dashboard_selection,
    select_invocation,
)
from crewplane.observability.tmux.selection_control import (
    SelectionControlState,
    read_selection_control,
)
from crewplane.observability.tmux.session_lifecycle import StartedCompactSession
from crewplane.observability.tmux.window import TmuxCompactWindowOptions
from crewplane.observability.types import DashboardSnapshot, RunResult


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
        self._dashboard_generation = 0

    def reset(self) -> None:
        with self._snapshot_lock:
            self._latest_snapshot = None
            self._dashboard_generation = 0
        JSON_OBJECT_THROTTLE.clear_all()

    def on_snapshot(
        self, event: ExecutionEvent | None, snapshot: DashboardSnapshot
    ) -> None:
        if event is not None and not isinstance(event, ExecutionEvent):
            raise TypeError("event must be an ExecutionEvent instance or None")
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

        self._render_snapshot(session, snapshot)
        return RefreshOutcome()

    def render_terminal_result(
        self,
        session: StartedCompactSession,
        result: RunResult,
    ) -> RefreshOutcome:
        if not session.tmux.session_exists(session.targets.session_name):
            return RefreshOutcome(stop_reason=StopReason.SESSION_GONE)

        with self._snapshot_lock:
            snapshot = copy.deepcopy(self._latest_snapshot)
            if snapshot is None:
                return RefreshOutcome()
            snapshot.state.workflow_status = result.status
            if snapshot.state.workflow_finished_at is None:
                snapshot.state.workflow_finished_at = self._monotonic_now()
            self._latest_snapshot = copy.deepcopy(snapshot)

        self._render_snapshot(session, snapshot)
        return RefreshOutcome()

    def _render_snapshot(
        self,
        session: StartedCompactSession,
        snapshot: DashboardSnapshot,
    ) -> None:
        runtime_files = session.runtime_files
        targets = session.targets
        tmux = session.tmux
        state = snapshot.state
        self._dashboard_generation += 1
        selection_control = read_selection_control(runtime_files)
        selection = resolve_dashboard_selection(
            snapshot,
            selection_control.selected_index,
        )
        selected_node_id = selection.selected_node_id
        self._write_runtime_selection(
            runtime_files,
            selection,
            selection_control,
            state,
        )

        mode = read_runtime_mode(runtime_files.mode)
        inspect_snapshot = read_snapshot(runtime_files.inspect_invocation)
        inspect_node_id = _snapshot_string(inspect_snapshot, "node_id")
        inspect_view = _snapshot_string(inspect_snapshot, "inspect_view")
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
                inspect_view=inspect_view,
            ),
        )

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
        selection_control: SelectionControlState,
        state: RunDashboardState,
    ) -> None:
        write_atomic(runtime_files.node_count, str(len(selection.ordered_node_ids)))
        write_json_atomic(
            runtime_files.selected_invocation,
            self._selected_invocation_record(
                state,
                selection,
                selection_control,
            ),
        )

    def _selected_invocation_record(
        self,
        state: RunDashboardState,
        selection: DashboardSelection,
        selection_control: SelectionControlState,
    ) -> dict[str, object]:
        invocation = _selected_invocation(state, selection.selected_node_id)
        record: dict[str, object] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "workflow_name": state.workflow_name,
            "run_id": state.run_id,
            "dashboard_generation": self._dashboard_generation,
            "selection_generation": selection_control.selection_generation,
            "requested_selected_index": selection_control.selected_index,
            "resolved_selected_index": selection.selected_index,
            "node_count": len(selection.ordered_node_ids),
            "node_id": selection.selected_node_id,
            "written_at": self._wall_time_now(),
        }
        if invocation is None:
            return record
        record.update(
            {
                "task_id": invocation.task_id,
                "provider": invocation.provider,
                "role": invocation.role,
                "model": invocation.model,
                "audit_round_num": invocation.audit_round_num,
                "round_num": invocation.round_num,
                "invocation_status": invocation.status,
                "output_file": invocation.output_file,
                "log_file": invocation.log_file,
            }
        )
        if invocation.log_presentation_format is not None:
            record["log_presentation_format"] = invocation.log_presentation_format
        if invocation.log_presentation_profile is not None:
            record["log_presentation_profile"] = invocation.log_presentation_profile
        return record

    def _quit_requested(self, runtime_files: RuntimeFiles) -> bool:
        return read_runtime_value(runtime_files.quit_requested) == "1"


def _selected_invocation(
    state: RunDashboardState,
    selected_node_id: str | None,
) -> InvocationRuntimeState | None:
    if selected_node_id is None:
        return None
    node = state.nodes.get(selected_node_id)
    if node is None:
        return None
    return select_invocation(node)


def _snapshot_string(snapshot: dict[str, Any] | None, key: str) -> str | None:
    if snapshot is None:
        return None
    value = snapshot.get(key)
    return value if isinstance(value, str) else None
