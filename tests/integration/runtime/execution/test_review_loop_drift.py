import asyncio
from pathlib import Path

import pytest

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.artifacts.generated_files import generated_file_source_root
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.preflight.signatures import signature_for_payload
from orchestrator_cli.observability.events import (
    ExecutionEventContext,
    format_execution_event_log_line,
    invocation_event,
    runtime_log_event,
)
from orchestrator_cli.runtime.execution.common import (
    CompiledRuntimeContext,
    ProviderCallDisplay,
)
from orchestrator_cli.runtime.execution.review_loop import (
    drift as review_loop_drift,
)
from orchestrator_cli.runtime.execution.review_loop import (
    drift_detection as review_loop_drift_detection,
)
from orchestrator_cli.runtime.execution.review_loop.types import (
    ActivityWindow,
    DriftGuardCallRequest,
    DriftMonitoringWindow,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _request(tmp_path: Path) -> tuple[DriftGuardCallRequest, OutputManager, Path]:
    output = OutputManager("workflow", base_dir=tmp_path)
    agent_payload = AgentConfig(cli_cmd=["mock"], default_model="m1").model_dump(
        mode="json",
        exclude_none=True,
    )
    invoker_payload = {
        "capabilities": {},
        "implementation": "mock",
        "options": {},
        "resolved_identity": "mock",
    }
    node = PreflightExecutionNode(
        id="review.node",
        mode="sequential",
        artifact_contract=ArtifactContract(output_path="review.node-result.md"),
    )
    node_dir = output.create_stage_dir(node.id)
    request = DriftGuardCallRequest(
        runtime_context=CompiledRuntimeContext(
            plan=PreflightExecutionPlan(
                run_id="run-1",
                run_key_name="run-1",
                project_root=".",
                context_root=".",
                manifest_root=".orchestrator",
                created_at="2026-06-03T00:00:00",
                workflow_name="workflow",
                workflow_signature="workflow-signature",
                execution_order=["review.node"],
                nodes=[node],
                render_plans=[],
                static_resources=[],
                token_catalog=[],
                dependency_graph=[],
                runtime_config_snapshot={
                    "agents": {"exec": agent_payload},
                    "execution": {},
                    "invoker": {**invoker_payload, "option_scopes": {}},
                    "schema_version": SCHEMA_VERSION,
                },
                effective_runtime_config_signature="runtime-signature",
                fingerprint_metadata={"payload_version": "1"},
            ),
            secret_context=SecretContext(),
        ),
        output=output,
        node=node,
        node_dir=node_dir,
        invoker=object(),
        telemetry=None,
        audit_round_num=None,
        round_num=1,
        provider=ProviderRecord(
            provider="exec",
            role="executor",
            task_id="exec_executor_0",
            agent_config_key="exec",
            invoker_alias="mock",
            agent_config_signature=signature_for_payload(
                {
                    "agent_config": agent_payload,
                    "agent_config_key": "exec",
                }
            ),
            invoker_config_signature=signature_for_payload(invoker_payload),
        ),
        task_id="exec_executor_0",
        prompt="Prompt",
        output_file=node_dir / "exec_executor_0_round1.md",
        role_label="executor",
        findings_enabled=False,
        allowed_paths=set(),
        display=ProviderCallDisplay(
            telemetry=None,
            progress_description="Executing exec...",
        ),
    )
    return request, output, node_dir


def test_current_invocation_and_parallel_reviewer_outputs_are_allowed(
    tmp_path: Path,
) -> None:
    _, output, node_dir = _request(tmp_path)
    executor_output = node_dir / "exec_executor_0_round1.md"
    reviewer_output = node_dir / "review_reviewer_0_round1.md"

    drift = review_loop_drift_detection.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={executor_output: (1, "a"), reviewer_output: (1, "b")},
        allowed_paths={executor_output, reviewer_output},
        output=output,
        node_dir=node_dir,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_shared_reserved_drift_is_ignored_when_not_exclusive(tmp_path: Path) -> None:
    request, output, _ = _request(tmp_path)
    result_path = output.results_dir / "review.node-result.md"
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot={result_path: (1, "before")},
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=1),
    )

    drift = review_loop_drift_detection.detect_shared_reserved_drift(
        request,
        window,
        check_shared_reserved_drift=False,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


@pytest.mark.parametrize("is_exclusive", [True, False])
def test_summary_drift_is_always_fatal(tmp_path: Path, is_exclusive: bool) -> None:
    request, output, _node_dir = _request(tmp_path)
    summary_path = output.get_orchestrator_summary_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(b"after")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=b"before",
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=is_exclusive, version=1),
    )

    drift = review_loop_drift_detection.detect_summary_drift(request, window)

    assert drift.fatal_paths == (summary_path,)


@pytest.mark.parametrize("strict_expected_append", [True, False])
def test_event_log_destructive_drift_is_always_fatal(
    tmp_path: Path,
    strict_expected_append: bool,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"truncated",
        expected_append=b"",
        strict_expected_append=strict_expected_append,
    )

    assert drift.fatal_paths == (event_log_path,)


def test_event_log_absent_before_after_no_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"unexpected event\n",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_must_match(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    expected_append = b"appended\\n"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=expected_append,
        expected_append=expected_append,
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_empty_creation_is_fatal_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"",
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == (event_log_path,)


def test_event_log_creation_mismatch_is_ignored_when_not_strict(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"concurrent event\n",
        expected_append=b"expected event\n",
        strict_expected_append=False,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_append_mismatch_is_fatal_only_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    strict_drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=True,
    )
    non_strict_drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=False,
    )

    assert strict_drift.fatal_paths == (event_log_path,)
    assert non_strict_drift.fatal_paths == ()


def test_event_log_append_allows_ambient_runtime_warning(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    started = format_execution_event_log_line(
        invocation_event(
            "invocation_started",
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role="executor",
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    finished = format_execution_event_log_line(
        invocation_event(
            "invocation_finished",
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role="executor",
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    ambient_warning = format_execution_event_log_line(
        runtime_log_event(
            "workflow",
            "run-1",
            level="warning",
            message="tmux command timed out; live dashboard may be stale",
            operation="runtime_warning",
        )
    ).encode("utf-8")

    drift = review_loop_drift_detection.detect_event_log_drift(
        event_log_path,
        before=b'{"event":"baseline"}\n',
        after=b'{"event":"baseline"}\n' + started + ambient_warning + finished,
        expected_append=started + finished,
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_node_local_unexpected_writes_are_warning_level(tmp_path: Path) -> None:
    _, output, node_dir = _request(tmp_path)
    unexpected = node_dir / "review-state" / "mutated-note.md"

    drift = review_loop_drift_detection.detect_artifact_drift(
        before_snapshot={},
        after_snapshot={unexpected: (1, "hash")},
        allowed_paths=set(),
        output=output,
        node_dir=node_dir,
    )

    assert drift.warning_paths == (unexpected,)
    assert drift.fatal_paths == ()


def test_runtime_generated_file_source_snapshots_are_allowed(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    snapshot_root = generated_file_source_root(request.output_file)
    generated_source = snapshot_root / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    request.runtime_context.generated_file_workspaces.record(
        request.node.id,
        request.output_file,
        snapshot_root,
    )
    review_loop_drift.allow_runtime_generated_file_snapshots(request)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert generated_source in request.allowed_paths
    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_unregistered_generated_file_source_snapshots_are_drift(
    tmp_path: Path,
) -> None:
    request, _output, _node_dir = _request(tmp_path)
    generated_source = generated_file_source_root(request.output_file) / "src/app.txt"
    generated_source.parent.mkdir(parents=True)
    generated_source.write_text("generated", encoding="utf-8")
    review_loop_drift.allow_runtime_generated_file_snapshots(request)
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=None,
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=False, version=None),
    )

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )

    assert generated_source not in request.allowed_paths
    assert drift.warning_paths == (generated_source,)
    assert drift.fatal_paths == ()


def test_drift_is_checked_when_provider_call_fails(tmp_path: Path) -> None:
    request, _output, node_dir = _request(tmp_path)

    class MutatingFailingInvoker:
        def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
            return None

        async def invoke(  # type: ignore[no-untyped-def]
            self,
            config,  # noqa: ARG002 - Required by protocol.
            model,  # noqa: ARG002 - Required by protocol.
            prompt,  # noqa: ARG002 - Required by protocol.
            output_file,  # noqa: ARG002 - Required by protocol.
            cwd,  # noqa: ARG002 - Required by protocol.
            log_file=None,  # noqa: ARG002 - Required by protocol.
            invocation_context=None,  # noqa: ARG002 - Required by protocol.
        ) -> None:
            mutation_path = node_dir / "review-state" / "mutated-after-failure.md"
            mutation_path.parent.mkdir(parents=True, exist_ok=True)
            mutation_path.write_text("mutated", encoding="utf-8")
            raise RuntimeError("provider boom")

    request = DriftGuardCallRequest(
        runtime_context=request.runtime_context,
        output=request.output,
        node=request.node,
        node_dir=request.node_dir,
        invoker=MutatingFailingInvoker(),
        telemetry=request.telemetry,
        audit_round_num=request.audit_round_num,
        round_num=request.round_num,
        provider=request.provider,
        task_id=request.task_id,
        prompt=request.prompt,
        output_file=request.output_file,
        role_label=request.role_label,
        findings_enabled=request.findings_enabled,
        allowed_paths=request.allowed_paths,
        display=request.display,
    )

    with pytest.raises(RuntimeError, match="provider boom") as exc_info:
        asyncio.run(review_loop_drift.run_provider_call_with_drift_guard(request))

    notes = getattr(exc_info.value, "__notes__", [])
    assert any("artifact drift detected" in note for note in notes)
