from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import crewplane.runtime.execution.workflow.cleanup as workflow_cleanup_module
import crewplane.runtime.execution.workflow.execution_session as workflow_execution_session_module
import crewplane.runtime.execution.workflow.node as workflow_node_module
import crewplane.runtime.execution.workflow.orchestration as workflow_module
import crewplane.runtime.execution.workflow.postconditions as workflow_postconditions_module
from crewplane.architecture.contracts import EventType, is_workflow_event_type
from crewplane.architecture.ports.artifacts import StageFinalizeResult
from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceRegistry,
)
from tests.integration.runtime.execution.workflow.workflow_cleanup_support import (
    empty_cleanup_plan,
    single_node_cleanup_plan,
)


def test_successful_scheduler_becomes_failure_when_workspace_ref_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_cleanup(plan: PreflightExecutionPlan) -> int:
        raise RuntimeError(f"cleanup failed for {plan.run_key_name}")

    monkeypatch.setattr(
        workflow_cleanup_module,
        "cleanup_plan_workspace_refs",
        fail_cleanup,
    )
    output = OutputManager("Workflow", base_dir=tmp_path)
    events: list[ExecutionEvent] = []

    with pytest.raises(RuntimeError, match="Workspace reference cleanup failed"):
        asyncio.run(
            workflow_module.execute_workflow(
                empty_cleanup_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                event_sink=events.append,
                suppress_progress_output=True,
            )
        )

    cleanup_warnings = [
        event
        for event in events
        if event.event_type == EventType.RUNTIME_LOG
        and event.payload.operation == "workspace_ref_cleanup"
    ]
    assert len(cleanup_warnings) == 1
    assert cleanup_warnings[0].payload.level == "warning"
    assert not any(is_workflow_event_type(event.event_type) for event in events[1:])


def test_successful_node_cleanup_retains_failed_generated_file_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class GeneratedFileRegistry:
        def __init__(self) -> None:
            self.retain_flags: list[bool] = []

        def roots_for_node(self, node_id: str) -> dict[Path, Path]:
            assert node_id == "input"
            return {}

        def cleanup_node_best_effort(
            self,
            node_id: str,
            retain_failed_callbacks: bool = True,
        ) -> tuple[Exception, ...]:
            assert node_id == "input"
            self.retain_flags.append(retain_failed_callbacks)
            return (RuntimeError("cleanup failed"),)

        async def cleanup_node_best_effort_async(
            self,
            node_id: str,
            retain_failed_callbacks: bool = True,
        ) -> tuple[Exception, ...]:
            return self.cleanup_node_best_effort(node_id, retain_failed_callbacks)

    class Output:
        def finalize_node(self, *args: object, **kwargs: object) -> StageFinalizeResult:
            del args, kwargs
            result_file = tmp_path / "input.md"
            result_file.write_text("input\n", encoding="utf-8")
            return StageFinalizeResult(
                stage_name="input",
                result_file=result_file,
                findings_file=None,
                included_outputs=(),
                skipped_empty_outputs=(),
                warnings=(),
            )

    def execute_input_stage(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def ignore_stage_finalize_logs(*args: object) -> None:
        del args

    def ignore_successful_node_state(*args: object) -> Path:
        del args
        state_file = tmp_path / "node-state.json"
        state_file.write_text("{}\n", encoding="utf-8")
        return state_file

    monkeypatch.setattr(
        workflow_node_module,
        "execute_input_stage",
        execute_input_stage,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "emit_stage_finalize_logs",
        ignore_stage_finalize_logs,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "write_successful_node_state",
        ignore_successful_node_state,
    )
    registry = GeneratedFileRegistry()
    runtime_context = SimpleNamespace(
        plan=empty_cleanup_plan(OutputManager("Workflow", base_dir=tmp_path)),
        generated_file_workspaces=registry,
        runtime_publications=RuntimePublicationRegistry(),
    )
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input-stage",
            output_path="input.md",
            log_path="input-stage/logs",
            result_path="input.md",
        ),
        input_content_ref="static-files/input.txt",
    )

    asyncio.run(
        workflow_node_module.execute_node(
            node,
            Output(),
            invoker=object(),
            runtime_context=runtime_context,
            telemetry=None,
            workflow_identity="workflow",
        )
    )

    assert registry.retain_flags == [True]


def test_execute_node_generated_file_cleanup_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_execute_node_generated_file_cleanup_does_not_block_event_loop(
            monkeypatch,
            tmp_path,
        )
    )


def test_workflow_reports_deferred_workspace_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    events: list[ExecutionEvent] = []
    drained_timeouts: list[float] = []

    class GeneratedFileRegistry:
        def cleanup_all(self) -> SimpleNamespace:
            return SimpleNamespace(errors=(), cleaned_node_ids=())

    class WorktreeReuseCache:
        def cleanup_all(self) -> SimpleNamespace:
            return SimpleNamespace(errors=(), updated_state_paths=())

    class DeferredWorkspaceCleanups:
        async def drain(self, timeout_seconds: float) -> tuple[Exception, ...]:
            drained_timeouts.append(timeout_seconds)
            return (RuntimeError("deferred cleanup failed"),)

    class RuntimeContext:
        def __init__(
            self,
            plan: PreflightExecutionPlan,
            secret_context: SecretContext,
        ) -> None:
            del secret_context
            self.plan = plan
            self.generated_file_workspaces = GeneratedFileRegistry()
            self.worktree_reuse_cache = WorktreeReuseCache()
            self.deferred_workspace_cleanups = DeferredWorkspaceCleanups()
            self.runtime_publications = RuntimePublicationRegistry()

        def validate_execution_contract(self) -> None:
            return None

        def max_concurrent_nodes(self) -> int | None:
            return None

        def max_parallel_invocations(self) -> int | None:
            return None

    def cleanup_refs(plan: PreflightExecutionPlan) -> int:
        del plan
        return 0

    monkeypatch.setattr(
        workflow_execution_session_module,
        "CompiledRuntimeContext",
        RuntimeContext,
    )
    monkeypatch.setattr(
        workflow_cleanup_module,
        "cleanup_plan_workspace_refs",
        cleanup_refs,
    )

    with pytest.raises(
        RuntimeError,
        match="Workflow postconditions failed.*deferred cleanup failed",
    ):
        asyncio.run(
            workflow_module.execute_workflow(
                empty_cleanup_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                event_sink=events.append,
                suppress_progress_output=True,
            )
        )

    assert drained_timeouts == [
        workflow_postconditions_module.DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS
    ]
    cleanup_warnings = [
        event
        for event in events
        if event.event_type == EventType.RUNTIME_LOG
        and event.payload.operation == "workspace_preparation_cancellation_cleanup"
    ]
    assert len(cleanup_warnings) == 1
    assert cleanup_warnings[0].payload.level == "warning"
    assert (
        "deferred cleanup failed"
        in cleanup_warnings[0].payload.attributes["first_error"]
    )
    assert not any(event.event_type == EventType.WORKFLOW_FINISHED for event in events)


async def _run_execute_node_generated_file_cleanup_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    registry = GeneratedFileWorkspaceRegistry()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocking_cleanup() -> None:
        loop.call_soon_threadsafe(cleanup_started.set)
        assert asyncio.run_coroutine_threadsafe(
            release_cleanup.wait(),
            loop,
        ).result(timeout=2)

    def execute_input_stage(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def ignore_stage_finalize_logs(*args: object) -> None:
        del args

    def ignore_successful_node_state(*args: object) -> Path:
        del args
        state_file = tmp_path / "node-state.json"
        state_file.write_text("{}\n", encoding="utf-8")
        return state_file

    monkeypatch.setattr(
        workflow_node_module,
        "execute_input_stage",
        execute_input_stage,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "emit_stage_finalize_logs",
        ignore_stage_finalize_logs,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "write_successful_node_state",
        ignore_successful_node_state,
    )
    result_file = tmp_path / "input-result.md"
    result_file.write_text("input\n", encoding="utf-8")

    def finalize_node(*args: object, **kwargs: object) -> StageFinalizeResult:
        del args, kwargs
        return StageFinalizeResult(
            stage_name="input",
            result_file=result_file,
            findings_file=None,
            included_outputs=(),
            skipped_empty_outputs=(),
            warnings=(),
        )

    monkeypatch.setattr(output, "finalize_node", finalize_node)

    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input-stage",
            output_path="input.md",
            log_path="input-stage/logs",
            result_path="input.md",
        ),
    )
    runtime_context = SimpleNamespace(
        plan=single_node_cleanup_plan(output),
        generated_file_workspaces=registry,
        runtime_publications=RuntimePublicationRegistry(),
    )
    registry.record("input", tmp_path / "output.md", None, blocking_cleanup)
    task = asyncio.create_task(
        workflow_node_module.execute_node(
            node,
            output,
            invoker=object(),
            runtime_context=runtime_context,
            telemetry=None,
            workflow_identity="workflow",
        )
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
        assert not task.done()
        release_cleanup.set()
        await task
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert registry.cleanup_by_node == {}
