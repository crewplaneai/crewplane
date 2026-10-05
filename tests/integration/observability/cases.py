from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from crewplane.architecture.contracts import EventType
from crewplane.core.workflow.models import WorkflowPlan


@dataclass(frozen=True)
class VisualizationCase:
    case_id: str
    build_workflow: Callable[[Path], WorkflowPlan]
    snapshot_event_type: EventType
    snapshot_node_id: str | None = None
    selected_node_id: str | None = None
    expected_fragments: tuple[str, ...] = ()
    unexpected_fragments: tuple[str, ...] = ()
    mock_options: Mapping[str, object] = field(default_factory=dict)
    expect_error: str | None = None
    expect_error_type: type[Exception] = RuntimeError
    render_width: int = 120
    expected_left_fragments: tuple[str, ...] = ()
    expected_right_fragments: tuple[str, ...] = ()
