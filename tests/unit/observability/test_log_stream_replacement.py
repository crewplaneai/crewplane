from __future__ import annotations

from pathlib import Path

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    InvocationRuntimeState,
    NodeRuntimeState,
    RunDashboardState,
)
from crewplane.observability.log_stream import NodeLogStreamTracker
from crewplane.observability.tmux.log_tail import read_log_snapshot, read_log_tail


def test_stream_tracker_handles_missing_unchanged_and_replaced_log(
    tmp_path: Path,
) -> None:
    log = tmp_path / "provider.log"
    node = NodeRuntimeState("node", "parallel", ("mock",))
    node.invocations["task"] = InvocationRuntimeState(
        "task",
        "mock",
        ProviderRole.EXECUTOR,
        None,
        None,
        None,
        status="running",
        log_file=str(log),
    )
    state = RunDashboardState("workflow", "run", {"node": 0}, nodes={"node": node})
    tracker = NodeLogStreamTracker(2)
    tracker.refresh(state)
    assert tracker.get_node_lines() == {}

    log.write_text("old complete\nunfinished old line", encoding="utf-8")
    tracker.refresh(state)
    tracker.refresh(state)
    assert tracker.get_node_lines() == {"node": ["old complete"]}

    log.write_text("new\n\n", encoding="utf-8")
    tracker.refresh(state)
    assert tracker.get_node_lines() == {"node": ["old complete", "new"]}


def test_tail_handles_missing_and_empty_logs(tmp_path: Path) -> None:
    log = tmp_path / "provider.log"
    assert read_log_snapshot(log, 5, 0) is None
    assert read_log_tail(log, 5) == []
    log.touch()
    assert read_log_tail(log, 0) == []
    assert read_log_tail(log, 5) == []
    snapshot = read_log_snapshot(log, 5, 0)
    assert snapshot is not None
    assert snapshot.size_bytes == 0
    assert snapshot.tail_lines == ()


def test_large_log_tail_keeps_long_unterminated_first_line(tmp_path: Path) -> None:
    log = tmp_path / "provider.log"
    body = "x" * 70000
    log.write_text(body, encoding="utf-8")

    assert read_log_tail(log, 3) == [body]
