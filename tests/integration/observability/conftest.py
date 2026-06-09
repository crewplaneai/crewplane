from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from rich.console import Console

from orchestrator_cli.bootstrap.container import build_runtime_components
from orchestrator_cli.core.config import Config, load_config
from orchestrator_cli.core.workflow_models import WorkflowPlan
from orchestrator_cli.observability import PersistentRunLogger, render_dag_summary
from orchestrator_cli.observability.runtime import ObservabilityHub
from orchestrator_cli.observability.types import (
    DashboardSnapshot,
    RunContext,
    RunResult,
)
from orchestrator_cli.runtime.execution.workflow import execute_workflow
from tests.helpers.observability import topology_from_workflow
from tests.integration.compiled_plan_helpers import compile_plan_for_components
from tests.integration.observability.dag_render_case_fixtures import (
    STATUS_CASES,
    TOPOLOGY_CASES,
    CaseData,
)

CONFIG_TEMPLATE_PATH = Path(__file__).with_name("fixtures") / "config.yml"


@dataclass(frozen=True)
class RecordedSnapshot:
    event_type: str | None
    node_id: str | None
    snapshot: DashboardSnapshot


class SnapshotRecorder:
    def __init__(self) -> None:
        self.context: RunContext | None = None
        self.result: RunResult | None = None
        self.snapshots: list[RecordedSnapshot] = []

    def start(self, context: RunContext) -> None:
        self.context = context

    def on_snapshot(self, event, snapshot) -> None:  # type: ignore[no-untyped-def]
        self.snapshots.append(
            RecordedSnapshot(
                event_type=None if event is None else event.event_type,
                node_id=None if event is None else event.context.node_id,
                snapshot=snapshot,
            )
        )

    def stop(self, result: RunResult) -> None:
        self.result = result


@dataclass(frozen=True)
class VisualizationCase:
    case_id: str
    build_workflow: Callable[[Path], WorkflowPlan]
    snapshot_event_type: str
    snapshot_node_id: str | None = None
    selected_node_id: str | None = None
    expected_fragments: tuple[str, ...] = ()
    unexpected_fragments: tuple[str, ...] = ()
    mock_options: Mapping[str, object] = field(default_factory=dict)
    expect_error: str | None = None
    render_width: int = 120


@dataclass(frozen=True)
class ObservabilityRunResult:
    case: VisualizationCase
    workflow: WorkflowPlan
    selected_node_id: str | None
    selected_snapshot: DashboardSnapshot
    rendered: str
    snapshots: tuple[RecordedSnapshot, ...]
    error: Exception | None
    event_log_path: Path
    summary_path: Path
    stages_dir: Path


ALLOWED_CASE_METADATA_KEYS = frozenset(
    {
        "expected_left_fragments",
        "expected_right_fragments",
    }
)


def _case_id(case_data: CaseData) -> str:
    return str(case_data["case_id"])


@pytest.fixture(params=TOPOLOGY_CASES, ids=_case_id)
def dag_render_topology_case(request: pytest.FixtureRequest) -> CaseData:
    return cast(CaseData, request.param)


@pytest.fixture(params=STATUS_CASES, ids=_case_id)
def dag_render_status_case(request: pytest.FixtureRequest) -> CaseData:
    return cast(CaseData, request.param)


def _load_case_config(
    project_root: Path,
    workflow: WorkflowPlan,
    mock_options: Mapping[str, object],
) -> Config:
    with CONFIG_TEMPLATE_PATH.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise AssertionError(
            f"Config fixture must deserialize to a mapping: {CONFIG_TEMPLATE_PATH}"
        )

    invoker_options = (
        data["settings"]["integrations"]["invoker"]["options"]  # type: ignore[index]
    )
    if not isinstance(invoker_options, dict):
        raise AssertionError("Config fixture mock invoker options must be a mapping.")
    invoker_options.update(dict(mock_options))

    config_path = project_root / ".orchestrator" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    config = load_config(config_path)

    missing_providers = sorted(
        {
            provider.provider
            for node in workflow.nodes
            for provider in node.providers
            if provider.provider not in config.agents
        }
    )
    if missing_providers:
        raise AssertionError(
            "Config fixture does not define agents for providers: "
            f"{missing_providers!r}"
        )
    return config


def _select_snapshot(
    snapshots: tuple[RecordedSnapshot, ...],
    event_type: str,
    node_id: str | None,
) -> DashboardSnapshot:
    for recorded_snapshot in reversed(snapshots):
        if recorded_snapshot.event_type != event_type:
            continue
        if node_id is not None and recorded_snapshot.node_id != node_id:
            continue
        return recorded_snapshot.snapshot

    available = [
        (recorded_snapshot.event_type, recorded_snapshot.node_id)
        for recorded_snapshot in snapshots
    ]
    raise AssertionError(
        "Could not find recorded snapshot for "
        f"event_type={event_type!r}, node_id={node_id!r}. "
        f"Available: {available!r}"
    )


def _assert_expected_outcome(case: VisualizationCase, error: Exception | None) -> None:
    if case.expect_error is None and error is None:
        return
    if case.expect_error is None and error is not None:
        raise AssertionError(
            f"Case '{case.case_id}' failed unexpectedly: {error}"
        ) from error
    if error is None:
        raise AssertionError(f"Case '{case.case_id}' was expected to fail.")
    if case.expect_error not in str(error):
        raise AssertionError(
            f"Case '{case.case_id}' failed with unexpected message: {error}"
        ) from error


@pytest.fixture
def run_visualization_case() -> Callable[
    [Path, Mapping[str, Any]], ObservabilityRunResult
]:
    case_field_names = frozenset(VisualizationCase.__dataclass_fields__)

    def _run(
        tmp_path: Path,
        case_data: Mapping[str, Any],
    ) -> ObservabilityRunResult:
        unknown_case_keys = (
            set(case_data) - case_field_names - ALLOWED_CASE_METADATA_KEYS
        )
        if unknown_case_keys:
            raise AssertionError(
                f"Unexpected visualization case fields: {sorted(unknown_case_keys)!r}"
            )
        case_kwargs = {
            key: value for key, value in case_data.items() if key in case_field_names
        }
        case = VisualizationCase(**case_kwargs)
        workflow = case.build_workflow(tmp_path)
        config = _load_case_config(tmp_path, workflow, case.mock_options)
        components = build_runtime_components(
            config=config,
            workflow_topology=topology_from_workflow(workflow),
            orchestrator_dir=tmp_path / ".orchestrator",
            project_root=tmp_path,
            console=Console(
                file=io.StringIO(),
                force_terminal=False,
                color_system=None,
                width=case.render_width,
            ),
            no_live=True,
        )
        recorder = SnapshotRecorder()
        persistent_logger = PersistentRunLogger(components.artifact_store)
        error: Exception | None = None

        try:
            with ObservabilityHub(
                workflow_topology=topology_from_workflow(workflow),
                run_id=components.artifact_store.run_id,
                observers=[recorder, persistent_logger],
                refresh_per_second=0,
            ) as hub:
                plan, secret_context = compile_plan_for_components(
                    config=config,
                    workflow=workflow,
                    components=components,
                    project_root=tmp_path,
                )
                asyncio.run(
                    execute_workflow(
                        plan=plan,
                        output=components.artifact_store,
                        invoker=components.base_invoker,
                        secret_context=secret_context,
                        event_sink=hub.emit,
                        run_id=components.artifact_store.run_id,
                        suppress_progress_output=True,
                    )
                )
        except Exception as exc:  # pragma: no cover - exercised by failure cases
            error = exc

        _assert_expected_outcome(case, error)

        snapshots = tuple(recorder.snapshots)
        event_log_path = components.artifact_store.get_orchestrator_event_log_path()
        summary_path = components.artifact_store.get_orchestrator_summary_path()
        assert event_log_path.exists()
        assert summary_path.exists()

        selected_snapshot = _select_snapshot(
            snapshots,
            case.snapshot_event_type,
            case.snapshot_node_id,
        )
        selected_node_id = case.selected_node_id
        if selected_node_id is None and workflow.nodes:
            selected_node_id = workflow.nodes[0].id

        rendered = "\n".join(
            render_dag_summary(
                state=selected_snapshot.state,
                layout=selected_snapshot.layout,
                selected_node_id=selected_node_id,
                width=case.render_width,
                now=selected_snapshot.now,
            )
        )

        return ObservabilityRunResult(
            case=case,
            workflow=workflow,
            selected_node_id=selected_node_id,
            selected_snapshot=selected_snapshot,
            rendered=rendered,
            snapshots=snapshots,
            error=error,
            event_log_path=event_log_path,
            summary_path=summary_path,
            stages_dir=components.artifact_store.stages_dir,
        )

    return _run
