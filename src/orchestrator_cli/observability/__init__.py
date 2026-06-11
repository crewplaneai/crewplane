from typing import TYPE_CHECKING

from .dag_render import DagRenderConfig, render_dag_summary
from .events import (
    EventSink,
    ExecutionEvent,
    apply_event,
    build_initial_state,
    emit_event,
)
from .layout import NodePlacement, TopologyLayout, compute_topology_layout
from .log_stream import MAX_STREAM_LINES_PER_NODE, NodeLogStreamTracker
from .persistent import PersistentRunLogger
from .render import RenderConfig, render_dashboard_text
from .runtime import ObservabilityHub
from .types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
    TopologyNode,
    TopologyProvider,
    WorkflowTopology,
)

# Keep top-level observability imports light; tmux is loaded on first export access.
if TYPE_CHECKING:
    from .tmux import TmuxCompactRuntime, build_attach_command

__all__ = [
    "DagRenderConfig",
    "DashboardSnapshot",
    "EventSink",
    "ExecutionEvent",
    "MAX_STREAM_LINES_PER_NODE",
    "NodeLogStreamTracker",
    "NodePlacement",
    "ObservabilityHub",
    "PersistentRunLogger",
    "RenderConfig",
    "RunContext",
    "RunResult",
    "TmuxCompactRuntime",
    "TopologyLayout",
    "TopologyNode",
    "TopologyProvider",
    "WorkflowTopology",
    "apply_event",
    "build_attach_command",
    "build_initial_state",
    "compute_topology_layout",
    "emit_event",
    "render_dag_summary",
    "render_dashboard_text",
]


def __getattr__(name: str) -> object:
    if name in {"TmuxCompactRuntime", "build_attach_command"}:
        from .tmux import TmuxCompactRuntime, build_attach_command

        exports = {
            "TmuxCompactRuntime": TmuxCompactRuntime,
            "build_attach_command": build_attach_command,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
