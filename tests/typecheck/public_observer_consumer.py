from __future__ import annotations

from typing import assert_type

from crewplane.architecture.contracts import (
    DashboardSnapshot,
    ExecutionEvent,
    ObserverCapabilities,
    RunContext,
    RunResult,
    RuntimeObserver,
)
from crewplane.architecture.ports.runtime import UIRuntimePlan


class ExternalObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: RunContext) -> None:  # noqa: ARG002 - Observer protocol.
        pass

    def on_snapshot(
        self,
        event: ExecutionEvent | None,  # noqa: ARG002 - Observer protocol.
        snapshot: DashboardSnapshot,
    ) -> None:
        workflow_name: str = snapshot.state.workflow_name
        workflow_status: str = snapshot.state.workflow_status
        ordered_nodes: list[str] = [
            node_id
            for wave in snapshot.layout.waves
            for node_id in wave
            if node_id in snapshot.state.nodes
        ]
        lane_count: int = snapshot.layout.lane_count
        placements: list[tuple[str, int]] = [
            (placement.node_id, placement.wave_index)
            for placement in snapshot.layout.placements.values()
        ]
        first_log: str | None = None
        for node in snapshot.state.nodes.values():
            for invocation in node.invocations.values():
                first_log = invocation.log_file
                break
            if first_log is not None:
                break
        assert_type(workflow_name, str)
        assert_type(workflow_status, str)
        assert_type(ordered_nodes, list[str])
        assert_type(lane_count, int)
        assert_type(placements, list[tuple[str, int]])
        assert_type(first_log, str | None)

    def stop(self, result: RunResult) -> None:  # noqa: ARG002 - Observer protocol.
        pass


class IncompatibleObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: int) -> None:  # noqa: ARG002 - Observer protocol.
        pass

    def on_snapshot(  # noqa: ARG002 - Observer protocol.
        self, event: str | None, snapshot: bytes
    ) -> None:
        pass

    def stop(self, result: float) -> None:  # noqa: ARG002 - Observer protocol.
        pass


def install_observer(
    observer: RuntimeObserver,  # noqa: ARG001 - Static compatibility fixture.
) -> None:
    pass


install_observer(ExternalObserver())
install_observer(IncompatibleObserver())  # type: ignore[arg-type]
UIRuntimePlan(
    observers=(IncompatibleObserver(),),  # type: ignore[arg-type]
    suppress_progress_output=False,
)
