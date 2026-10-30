from types import SimpleNamespace
from unittest.mock import Mock

from crewplane.architecture.contracts import (
    EventType,
    ExecutionStatus,
    ObserverCapabilities,
    TopologyNode,
    WorkflowTopology,
)
from crewplane.architecture.contracts.execution_event import TopologyExpansionPayload
from crewplane.observability import ObservabilityHub
from crewplane.observability.events import (
    apply_event,
    build_initial_state,
    event_from_record,
    execution_event_log_record,
)
from crewplane.observability.events.builders import topology_expansion_event


def topology() -> WorkflowTopology:
    return WorkflowTopology(
        "workflow",
        (
            TopologyNode("before", "input"),
            TopologyNode("chunks", "foreach", ("before",)),
        ),
    )


def expansion():
    return topology_expansion_event(
        "workflow",
        "run",
        TopologyExpansionPayload(
            "chunks",
            1,
            (
                TopologyNode("chunks.i0.item", "input", ("before",)),
                TopologyNode("chunks.i0.work", "parallel", ("chunks.i0.item",)),
            ),
            ("before", "chunks.i0.work"),
        ),
    )


def test_topology_roundtrip_and_replay_preserve_progress() -> None:
    state = build_initial_state(topology(), "run")
    state.nodes["before"].status = ExecutionStatus.SUCCEEDED
    record = execution_event_log_record(expansion())
    event = event_from_record(record)
    assert event is not None and event.event_type == EventType.TOPOLOGY_EXPANDED
    assert not {"items", "prompt", "item"} & record.keys()
    apply_event(state, event)
    state.nodes["chunks.i0.work"].status = ExecutionStatus.RUNNING
    apply_event(state, event)
    assert list(state.nodes) == ["before", "chunks", "chunks.i0.item", "chunks.i0.work"]
    assert len(state.node_order) == 4
    assert state.nodes["before"].status == ExecutionStatus.SUCCEEDED
    assert state.nodes["chunks.i0.work"].status == ExecutionStatus.RUNNING
    assert state.topology_nodes["chunks"].dependencies == ("before", "chunks.i0.work")


def test_topology_reader_rejects_invalid_count_or_additions() -> None:
    record = execution_event_log_record(expansion())
    for value in (True, -1, "1"):
        assert event_from_record({**record, "item_count": value}) is None
    assert event_from_record({**record, "additions": [{}, {}]}) is None


def test_hub_recomputes_layout_for_dynamic_children() -> None:
    observer = SimpleNamespace(
        capabilities=ObserverCapabilities(synchronous_snapshot_delivery=True),
        start=Mock(),
        stop=Mock(),
        on_snapshot=Mock(),
        stop_requested=False,
    )
    with ObservabilityHub(topology(), "run", [observer], refresh_per_second=0) as hub:
        hub.emit(expansion())
        snapshot = observer.on_snapshot.call_args.args[1]
        assert set(snapshot.state.node_order) == {
            "before",
            "chunks",
            "chunks.i0.item",
            "chunks.i0.work",
        }
