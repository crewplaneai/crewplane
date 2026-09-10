import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import EventType
from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.observability.events import ExecutionEvent
from crewplane.observability.persistent import PersistentRunLogger
from crewplane.observability.types import RunContext
from crewplane.runtime.execution.common import (
    ExecutionTelemetry,
)
from crewplane.runtime.execution.provider_call import (
    generated_files as provider_generated_files,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.observability import topology_from_workflow
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    FailingLogOutputManager,
    ParallelReviewerBarrierInvoker,
    TaskOutputInvoker,
    execute_sequential_stage,
    review_loop_status_path,
    review_output,
)


class ParallelReviewerStreamingLogInvoker(ParallelReviewerBarrierInvoker):
    def __init__(self) -> None:
        super().__init__()
        self.slow_log_ready = asyncio.Event()
        self.release_slow_reviewer = asyncio.Event()
        self.slow_log_path: Path | None = None

    async def invoke(
        self,
        config: AgentConfig,  # noqa: ARG002 - Required by test double signature.
        model: str,  # noqa: ARG002 - Required by test double signature.
        prompt: str,  # noqa: ARG002 - Required by test double signature.
        output_file: Path,
        cwd: Path,  # noqa: ARG002 - Required by test double signature.
        log_file: Path | None = None,
        invocation_context=None,  # type: ignore[no-untyped-def]
    ) -> None:
        assert invocation_context is not None
        if invocation_context.role == ProviderRole.EXECUTOR:
            output_file.write_text("executor output", encoding="utf-8")
            return
        if invocation_context.task_id == "review-b_reviewer_1":
            assert log_file is not None
            self.slow_log_path = log_file
            log_file.write_text("review-b streaming", encoding="utf-8")
            self.slow_log_ready.set()
            await asyncio.wait_for(self.release_slow_reviewer.wait(), timeout=1.0)
        else:
            await asyncio.wait_for(self.slow_log_ready.wait(), timeout=1.0)
            asyncio.get_running_loop().call_later(
                0.05,
                self.release_slow_reviewer.set,
            )
        output_file.write_text(review_output(verdict="NO_FINDINGS"), encoding="utf-8")


class ExecutorSequentialStageBasicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_provider_reviewers_run_in_parallel_within_local_round(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.parallel.reviewers",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = ParallelReviewerBarrierInvoker()
            output = OutputManager("workflow", base_dir=tmp_path)

            await execute_sequential_stage(config, node, output, invoker=invoker)

            assert invoker.started_reviewer_task_ids == {
                "review-a_reviewer_0",
                "review-b_reviewer_1",
            }

    async def test_parallel_reviewer_peer_log_is_not_read_by_drift_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.parallel.logs",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = ParallelReviewerStreamingLogInvoker()
            output = OutputManager(
                "workflow",
                base_dir=tmp_path,
                log_cli_output=True,
            )
            original_read_bytes = Path.read_bytes
            peer_log_read_count = 0

            def append_after_peer_log_read(path: Path) -> bytes:
                nonlocal peer_log_read_count
                payload = original_read_bytes(path)
                if path == invoker.slow_log_path:
                    peer_log_read_count += 1
                    with path.open("ab") as stream:
                        stream.write(b" still running")
                return payload

            with patch.object(Path, "read_bytes", new=append_after_peer_log_read):
                await execute_sequential_stage(config, node, output, invoker=invoker)

            assert invoker.slow_log_path is not None
            assert peer_log_read_count == 0

    async def test_parallel_reviewer_private_snapshots_do_not_overlap(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.parallel.metadata",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            invoker = TaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                    "review-b_reviewer_1": review_output(verdict="NO_FINDINGS"),
                },
            )
            output = OutputManager("workflow", base_dir=tmp_path)
            persistent_logger = PersistentRunLogger(output)
            persistent_logger.start(
                RunContext(
                    workflow_topology=topology_from_workflow(
                        WorkflowPlan(name="workflow", nodes=[node])
                    ),
                    run_id=output.run_id,
                    refresh_per_second=0,
                )
            )
            events: list[ExecutionEvent] = []

            def record_event(event: ExecutionEvent) -> None:
                events.append(event)
                persistent_logger.record_event(event)

            snapshot_output_paths: list[Path] = []
            snapshot_roots: list[Path] = []
            original_snapshot = (
                provider_generated_files.snapshot_generated_file_workspace
            )

            def coordinate_snapshot(*args, **kwargs):  # type: ignore[no-untyped-def]
                snapshot_root = original_snapshot(*args, **kwargs)
                snapshot_output_paths.append(args[0])
                snapshot_roots.append(snapshot_root)
                return snapshot_root

            with patch.object(
                provider_generated_files,
                "snapshot_generated_file_workspace",
                side_effect=coordinate_snapshot,
            ):
                await execute_sequential_stage(
                    config,
                    node,
                    output,
                    invoker=invoker,
                    telemetry=ExecutionTelemetry(
                        workflow_name="workflow",
                        run_id=output.run_id,
                        event_sink=record_event,
                    ),
                )

            reviewer_snapshot_paths = [
                path
                for path in snapshot_output_paths
                if path.name == "provider-output.md"
            ]
            assert len(reviewer_snapshot_paths) == 2
            assert (
                reviewer_snapshot_paths[0].parent != reviewer_snapshot_paths[1].parent
            )
            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            reviewer_snapshot_roots = [
                root
                for source, root in zip(
                    snapshot_output_paths, snapshot_roots, strict=True
                )
                if source.name == "provider-output.md"
            ]
            assert all(root.exists() for root in reviewer_snapshot_roots)
            assert set(reviewer_snapshot_roots) == {
                provider_generated_files.generated_file_source_root(
                    node_dir / "review-a_reviewer_0_round1.md"
                ),
                provider_generated_files.generated_file_source_root(
                    node_dir / "review-b_reviewer_1_round1.md"
                ),
            }
            status_payload = json.loads(
                review_loop_status_path(node_dir).read_text(encoding="utf-8")
            )
            assert status_payload["artifact_drift_warning_count"] == 0

    async def test_parallel_reviewer_log_setup_failure_uses_invocation_lifecycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = Config(
                version=SCHEMA_VERSION,
                agents={
                    "exec": AgentConfig(cli_cmd=["mock"], default_model="exec-model"),
                    "review-a": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-a-model",
                    ),
                    "review-b": AgentConfig(
                        cli_cmd=["mock"],
                        default_model="review-b-model",
                    ),
                },
            )
            node = WorkflowNode(
                id="review.node.reviewer.log.failure",
                mode="sequential",
                prompt_segments=[
                    PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
                ],
                providers=[
                    ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="review-a", role=ProviderRole.REVIEWER),
                    ProviderSpec(provider="review-b", role=ProviderRole.REVIEWER),
                ],
            )
            release_review_a = asyncio.Event()
            invoker = TaskOutputInvoker(
                outputs_by_task_id={
                    "exec_executor_0": "executor output",
                    "review-a_reviewer_0": review_output(verdict="NO_FINDINGS"),
                },
                gates_by_task_id={"review-a_reviewer_0": release_review_a},
            )

            def release_in_flight_reviewer() -> None:
                assert "review-a" in output.log_setup_providers, (
                    "review-a lifecycle had not started when review-b setup failed"
                )
                release_review_a.set()

            output = FailingLogOutputManager(
                "workflow",
                base_dir=tmp_path,
                failing_provider="review-b",
                on_failure=release_in_flight_reviewer,
            )
            events: list[ExecutionEvent] = []

            try:
                with pytest.raises(RuntimeError, match="log setup failed"):
                    await execute_sequential_stage(
                        config,
                        node,
                        output,
                        invoker=invoker,
                        telemetry=ExecutionTelemetry(
                            workflow_name="workflow",
                            run_id="run-1",
                            event_sink=events.append,
                        ),
                    )
            finally:
                release_review_a.set()

            node_dir = output.get_node_dir(node_artifact_request(node.id))
            if node_dir is None:
                self.fail("Expected node directory to be created")
            assert not (node_dir / "review-a_reviewer_0_round1.md").exists()
            failed_events = [
                event
                for event in events
                if event.event_type == EventType.INVOCATION_FAILED
                and event.context.provider == "review-b"
            ]
            assert len(failed_events) == 1
            assert failed_events[0].context.task_id == "review-b_reviewer_1"
            assert "log setup failed" in (failed_events[0].payload.error or "")
