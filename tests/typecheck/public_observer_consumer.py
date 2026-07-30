from __future__ import annotations

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

    def start(self, context: RunContext) -> None:
        del context

    def on_snapshot(
        self,
        event: ExecutionEvent | None,
        snapshot: DashboardSnapshot,
    ) -> None:
        del event
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
        del workflow_name, workflow_status, ordered_nodes, lane_count, placements
        del first_log

    def stop(self, result: RunResult) -> None:
        del result


class IncompatibleObserver:
    capabilities = ObserverCapabilities(required=True)

    @property
    def stop_requested(self) -> bool:
        return False

    def start(self, context: int) -> None:
        del context

    def on_snapshot(self, event: str | None, snapshot: bytes) -> None:
        del event, snapshot

    def stop(self, result: float) -> None:
        del result


def install_observer(observer: RuntimeObserver) -> None:
    del observer


install_observer(ExternalObserver())
install_observer(IncompatibleObserver())  # type: ignore[arg-type]
UIRuntimePlan(
    observers=(IncompatibleObserver(),),  # type: ignore[arg-type]
    suppress_progress_output=False,
)
