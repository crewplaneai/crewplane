from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeGuard, cast, get_args

from orchestrator_cli.architecture.contracts import OutputExtractionStatus
from orchestrator_cli.artifacts.atomic import atomic_write_text
from orchestrator_cli.artifacts.naming import (
    build_findings_filename,
    build_result_filename,
)
from orchestrator_cli.artifacts.run_history import RunHistoryRecord
from orchestrator_cli.core.preflight.models import PreflightExecutionPlan
from orchestrator_cli.observability.events import (
    ExecutionEvent,
    ExecutionEventContext,
    InvocationEventPayload,
    NodeEventPayload,
    RuntimeLogEventPayload,
    WorkflowEventPayload,
    WorkspaceEventPayload,
    apply_event,
    build_initial_state,
)
from orchestrator_cli.observability.events.types import EventType, LogLevel
from orchestrator_cli.observability.layout import compute_topology_layout
from orchestrator_cli.observability.persistent import (
    build_run_summary,
    render_run_summary_markdown,
)
from orchestrator_cli.observability.types import DashboardSnapshot, RunResult

from .topology import workflow_topology_from_plan

HISTORICAL_EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))
HISTORICAL_LOG_LEVELS: frozenset[str] = frozenset(get_args(LogLevel))


@dataclass(frozen=True)
class _HistoricalArtifactStore:
    source: RunHistoryRecord

    @property
    def run_id(self) -> str:
        return self.source.manifest.run_id

    @property
    def run_key_name(self) -> str:
        return self.source.manifest.run_key_name

    @property
    def task_name(self) -> str:
        return self.source.manifest.workflow_name

    @property
    def stages_dir(self) -> Path:
        return self.source.run_dir

    @property
    def results_dir(self) -> Path:
        return self.source.results_dir

    @property
    def logs_dir(self) -> Path:
        return self.source.run_dir / "logs"

    def get_orchestrator_event_log_path(self) -> Path:
        return self.logs_dir / "events.ndjson"

    def get_orchestrator_summary_path(self) -> Path:
        return self.logs_dir / "summary.md"

    def get_stage_output_path(self, stage_name: str) -> Path:
        return self.results_dir / build_result_filename(stage_name)

    def get_stage_findings_path(self, stage_name: str) -> Path:
        return self.results_dir / build_findings_filename(stage_name)


def refresh_historical_run_summary(
    plan: PreflightExecutionPlan,
    source: RunHistoryRecord,
) -> Path:
    artifact_store = _HistoricalArtifactStore(source)
    events = _read_history_events(artifact_store.get_orchestrator_event_log_path())
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
        artifact_store.get_orchestrator_summary_path(),
        render_run_summary_markdown(summary),
    )


def _run_result_status(source: RunHistoryRecord) -> str:
    status = source.manifest.status
    if status in {"failed", "cancelled"}:
        return status
    return "succeeded"


def _read_history_events(event_log_path: Path) -> list[ExecutionEvent]:
    if not event_log_path.is_file() or event_log_path.is_symlink():
        return []
    events: list[ExecutionEvent] = []
    try:
        lines = event_log_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        event = _event_from_line(line)
        if event is not None:
            events.append(event)
    return events


def _event_from_line(line: str) -> ExecutionEvent | None:
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return _event_from_record(record)


def _event_from_record(record: Mapping[str, object]) -> ExecutionEvent | None:
    event_type = record.get("event_type")
    workflow_name = _string(record.get("workflow_name"))
    run_id = _string(record.get("run_id"))
    timestamp_utc = _string(record.get("timestamp"))
    if (
        not _is_event_type(event_type)
        or workflow_name is None
        or run_id is None
        or timestamp_utc is None
    ):
        return None
    payload = _payload_from_record(event_type, record)
    if payload is None:
        return None
    try:
        return ExecutionEvent(
            event_type=event_type,
            workflow_name=workflow_name,
            run_id=run_id,
            context=_context_from_record(workflow_name, run_id, record),
            payload=payload,
            timestamp=_timestamp_value(timestamp_utc),
            timestamp_utc=timestamp_utc,
        )
    except (TypeError, ValueError):
        return None


def _payload_from_record(
    event_type: EventType,
    record: Mapping[str, object],
) -> (
    WorkflowEventPayload
    | NodeEventPayload
    | InvocationEventPayload
    | RuntimeLogEventPayload
    | WorkspaceEventPayload
    | None
):
    if event_type in {"workflow_started", "workflow_finished", "workflow_failed"}:
        return WorkflowEventPayload(error=_string(record.get("error")))
    if event_type in {"node_started", "node_finished", "node_failed", "node_blocked"}:
        return NodeEventPayload(error=_string(record.get("error")))
    if event_type in {"invocation_started", "invocation_finished", "invocation_failed"}:
        return InvocationEventPayload(
            duration_ms=_integer(record.get("duration_ms")),
            error=_string(record.get("error")),
            attempt_count=_integer(record.get("attempt_count")),
            cli_captured=_boolean(record.get("cli_captured")),
            output_extraction_status=cast(
                OutputExtractionStatus | None,
                _string(record.get("output_extraction_status")),
            ),
            provider_usage_status=_string(record.get("provider_usage_status")),
            provider_tokens=_integer_mapping(record.get("provider_tokens")),
            visible_estimate_tokens=_integer(record.get("visible_estimate_tokens")),
            visible_estimate_method=_string(record.get("visible_estimate_method")),
            visible_estimate_is_lower_bound=_boolean(
                record.get("visible_estimate_is_lower_bound")
            ),
            configured_cost_usd=_float(record.get("configured_cost_usd")),
            invocation_cost_confidence=_string(
                record.get("invocation_cost_confidence")
            ),
            usage_parse_error=_string(record.get("usage_parse_error")),
            failure_kind=_string(record.get("failure_kind")),
            failure_phase=_string(record.get("failure_phase")),
            failure_source=_string(record.get("failure_source")),
            failure_advice=_string(record.get("failure_advice")),
        )
    if event_type == "workspace_context_recorded":
        return WorkspaceEventPayload(
            status=_string(record.get("status")),
            workspace_kind=_string(record.get("workspace_kind")),
            workspace_logical_worktree_name=_string(
                record.get("workspace_logical_worktree_name")
            ),
            workspace_materialization=_string(record.get("workspace_materialization")),
            workspace_source_kind=_string(record.get("workspace_source_kind")),
            workspace_source_node_id=_string(record.get("workspace_source_node_id")),
            workspace_source_commit=_string(record.get("workspace_source_commit")),
            workspace_source_tree=_string(record.get("workspace_source_tree")),
            worktree_contract_mode=_string(record.get("worktree_contract_mode")),
            worktree_contract_schema_version=_string(
                record.get("worktree_contract_schema_version")
            ),
            workspace_state_path=_string(record.get("workspace_state_path")),
            workspace_writable=_boolean(record.get("workspace_writable")),
            workspace_lineage_producer=_boolean(
                record.get("workspace_lineage_producer")
            ),
            workspace_child_environment_required=_boolean(
                record.get("workspace_child_environment_required")
            ),
            workspace_child_environment_applied=_boolean(
                record.get("workspace_child_environment_applied")
            ),
        )
    level = _string(record.get("level"))
    message = _string(record.get("message"))
    operation = _string(record.get("operation"))
    if not _is_log_level(level) or message is None or operation is None:
        return None
    return RuntimeLogEventPayload(
        level=level,
        message=message,
        operation=operation,
        attributes=_runtime_log_attributes(record.get("attributes")),
        duration_ms=_integer(record.get("duration_ms")),
        error=_string(record.get("error")),
    )


def _context_from_record(
    workflow_name: str,
    run_id: str,
    record: Mapping[str, object],
) -> ExecutionEventContext:
    return ExecutionEventContext(
        workflow_name=workflow_name,
        run_id=run_id,
        node_id=_string(record.get("node_id")),
        provider=_string(record.get("provider")),
        role=_string(record.get("role")),
        model=_string(record.get("model")),
        task_id=_string(record.get("task_id")),
        audit_round_num=_integer(record.get("audit_round_num")),
        round_num=_integer(record.get("round_num")),
        output_file=_string(record.get("output_file")),
        log_file=_string(record.get("log_file")),
        log_presentation_format=_string(record.get("log_presentation_format")),
        log_presentation_profile=_string(record.get("log_presentation_profile")),
    )


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


def _timestamp_value(timestamp_utc: str) -> float:
    try:
        return datetime.fromisoformat(timestamp_utc).timestamp()
    except ValueError:
        return 0.0


def _is_event_type(value: object) -> TypeGuard[EventType]:
    return isinstance(value, str) and value in HISTORICAL_EVENT_TYPES


def _is_log_level(value: str | None) -> TypeGuard[LogLevel]:
    return isinstance(value, str) and value in HISTORICAL_LOG_LEVELS


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _integer_mapping(value: object) -> Mapping[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int | None] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        if item is not None and not (
            isinstance(item, int) and not isinstance(item, bool)
        ):
            return None
        result[key] = item
    return result


def _runtime_log_attributes(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _is_runtime_log_value(item):
            return None
        result[key] = item
    return result


def _is_runtime_log_value(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)
