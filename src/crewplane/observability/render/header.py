from __future__ import annotations

from crewplane.observability.events import RunDashboardState
from crewplane.observability.timing import format_elapsed_seconds


def header_line(state: RunDashboardState) -> str:
    elapsed_label = format_elapsed_seconds(state.elapsed_seconds)
    return (
        f"Workflow: {state.workflow_name} | Run: {state.run_id} | "
        f"Status: {state.workflow_status} | Elapsed: {elapsed_label}"
    )


def counters_line(state: RunDashboardState) -> str:
    return (
        "Nodes => "
        f"pending={state.pending_nodes} "
        f"running={state.running_nodes} "
        f"succeeded={state.succeeded_nodes} "
        f"blocked={state.blocked_nodes} "
        f"failed={state.failed_nodes}"
    )
