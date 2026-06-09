from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
)
from orchestrator_cli.observability.tmux.log_tail import LogSnapshot, read_log_snapshot
from orchestrator_cli.observability.tmux.selection import select_invocation


@dataclass(frozen=True)
class PreparedSelectedInvocation:
    invocation: InvocationRuntimeState
    log_snapshot: LogSnapshot | None = None
    log_unavailable_message: str | None = None


def prepare_selected_invocation(
    nodes: Mapping[str, NodeRuntimeState],
    selected_node_id: str | None,
    pane_height: int,
    log_tail_lines: int | None,
    wall_time_now: float,
) -> PreparedSelectedInvocation | None:
    if selected_node_id is None:
        return None

    invocation = select_invocation(nodes[selected_node_id])
    if invocation is None:
        return None
    if invocation.log_file is None:
        return PreparedSelectedInvocation(
            invocation=invocation,
            log_unavailable_message="Log file unavailable for this invocation.",
        )

    log_path = Path(invocation.log_file)
    if not log_path.exists():
        return PreparedSelectedInvocation(
            invocation=invocation,
            log_unavailable_message=f"Log file not found: {log_path}",
        )

    log_line_count = log_tail_lines or max(1, pane_height)
    log_snapshot = read_log_snapshot(
        log_path=log_path,
        line_count=log_line_count,
        wall_time_now=wall_time_now,
    )
    if log_snapshot is None:
        return PreparedSelectedInvocation(
            invocation=invocation,
            log_unavailable_message="Log file unavailable for this invocation.",
        )
    return PreparedSelectedInvocation(
        invocation=invocation,
        log_snapshot=log_snapshot,
    )
