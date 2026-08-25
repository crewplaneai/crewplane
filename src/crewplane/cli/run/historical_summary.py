from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import (
    ArtifactContract,
    NodeArtifactRequest,
)
from crewplane.artifacts.atomic import atomic_write_text
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.core.execution_state import TerminalRunStatus
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.observability.events import (
    ExecutionEvent,
    apply_event,
    build_initial_state,
    read_event_log,
)
from crewplane.observability.layout import compute_topology_layout
from crewplane.observability.persistent import (
    build_run_summary,
    render_run_summary_markdown,
)
from crewplane.observability.types import DashboardSnapshot, RunResult

from .topology import workflow_topology_from_plan


@dataclass(frozen=True)
class _HistoricalArtifactStore:
    source: RunHistoryRecord
    artifact_contracts: dict[str, ArtifactContract]

    @property
    def stages_dir(self) -> Path:
        return self.source.run_dir

    @property
    def logs_dir(self) -> Path:
        return self.source.run_dir / "logs"

    def get_run_event_log_path(self) -> Path:
        return self.logs_dir / "events.ndjson"

    def get_run_summary_path(self) -> Path:
        return self.logs_dir / "summary.md"

    def get_node_artifact_request(
        self,
        node_id: str,
    ) -> NodeArtifactRequest | None:
        contract = self.artifact_contracts.get(node_id)
        return None if contract is None else NodeArtifactRequest(node_id, contract)

    def get_node_output_path(self, request: NodeArtifactRequest) -> Path:
        return self.source.results_dir / request.contract.output_path


def refresh_historical_run_summary(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
) -> Path:
    artifact_store = _HistoricalArtifactStore(
        source,
        {node.id: node.artifact_contract for node in plan.nodes},
    )
    events = read_event_log(artifact_store.get_run_event_log_path())
    snapshot = _historical_dashboard_snapshot(plan, source, events)
    summary = build_run_summary(
        artifact_store=artifact_store,
        snapshot=snapshot,
        events=events,
        result=RunResult(status=_run_result_status(source)),
        fallback_workflow_name=source.manifest.workflow_name,
        fallback_run_id=source.manifest.run_id,
    )
    return atomic_write_text(
        artifact_store.get_run_summary_path(),
        render_run_summary_markdown(summary),
    )


def _run_result_status(source: RunHistoryRecord) -> TerminalRunStatus:
    status = source.manifest.status
    if status in {"failed", "cancelled"}:
        return status
    return "succeeded"


def _historical_dashboard_snapshot(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
    events: list[ExecutionEvent],
) -> DashboardSnapshot | None:
    if not events:
        return None
    topology = workflow_topology_from_plan(plan)
    state = build_initial_state(topology, source.manifest.run_id)
    for event in events:
        try:
            apply_event(state, event)
        except ValueError:
            continue
    return DashboardSnapshot(
        state=state,
        layout=compute_topology_layout(topology),
        now=max(event.timestamp for event in events),
    )
