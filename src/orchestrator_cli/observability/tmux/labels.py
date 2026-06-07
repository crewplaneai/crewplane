from __future__ import annotations

from orchestrator_cli.observability.events import RunDashboardState
from orchestrator_cli.observability.status_icons import status_icon
from orchestrator_cli.observability.timing import format_elapsed_seconds


def status_line(state: RunDashboardState) -> str:
    elapsed_label = format_elapsed_seconds(state.elapsed_seconds)
    workflow_icon = status_icon(state.workflow_status)
    return (
        f"{state.workflow_name} | run={state.run_id} | "
        f"{workflow_icon} {state.workflow_status} | {elapsed_label}"
    )


def counts_line(state: RunDashboardState) -> str:
    return (
        f"✅ {state.succeeded_nodes} "
        f"⏳ {state.running_nodes} "
        f"⏸ {state.pending_nodes} "
        f"⛔ {state.blocked_nodes} "
        f"❌ {state.failed_nodes}"
    )
