from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace

import pytest

import crewplane.runtime.execution.workflow as workflow_module
import crewplane.runtime.execution.workflow.cleanup as workflow_cleanup_module
import crewplane.runtime.execution.workflow.node as workflow_node_module
from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceRegistry,
)
from crewplane.version import SCHEMA_VERSION


def test_successful_workflow_keeps_success_when_workspace_ref_cleanup_fails(
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

    asyncio.run(
        workflow_module.execute_workflow(
            _empty_plan(output),
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
        if event.event_type == "runtime_log"
        and event.payload.operation == "workspace_ref_cleanup"
    ]
    assert len(cleanup_warnings) == 1
    assert cleanup_warnings[0].payload.level == "warning"
    assert events[-1].event_type == "workflow_finished"


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
        def finalize_stage(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return object()

    def execute_input_stage(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def ignore_stage_finalize_logs(*args: object) -> None:
        del args

    def ignore_successful_node_state(*args: object) -> None:
        del args

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
        plan=_empty_plan(OutputManager("Workflow", base_dir=tmp_path)),
        generated_file_workspaces=registry,
    )
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.md"),
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


def test_workflow_refreshes_generated_file_cleanup_node_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    main_thread_id = get_ident()
    generated_cleanup_thread_ids: list[int] = []
    worktree_cleanup_thread_ids: list[int] = []
    ref_cleanup_thread_ids: list[int] = []
    refresh_thread_ids: list[int] = []
    refreshed: list[str] = []

    class GeneratedFileRegistry:
        def cleanup_all(self) -> SimpleNamespace:
            generated_cleanup_thread_ids.append(get_ident())
            return SimpleNamespace(errors=(), cleaned_node_ids=("input",))

    class WorktreeReuseCache:
        def cleanup_all(self) -> SimpleNamespace:
            worktree_cleanup_thread_ids.append(get_ident())
            return SimpleNamespace(errors=(), updated_state_paths=())

    class DeferredWorkspaceCleanups:
        async def drain(self, timeout_seconds: float) -> tuple[Exception, ...]:
            del timeout_seconds
            return ()

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

        def validate_execution_contract(self) -> None:
            return None

        def max_concurrent_nodes(self) -> int | None:
            return None

        def max_parallel_invocations(self) -> int | None:
            return None

    async def execute_node(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def refresh_node_workspace_descriptor(
        node: PreflightExecutionNode,
        plan: PreflightExecutionPlan,
        output_store: OutputManager,
    ) -> Path:
        del plan, output_store
        refresh_thread_ids.append(get_ident())
        refreshed.append(node.id)
        return tmp_path / "node-state.json"

    def cleanup_refs(plan: PreflightExecutionPlan) -> int:
        del plan
        ref_cleanup_thread_ids.append(get_ident())
        return 0

    monkeypatch.setattr(workflow_module, "CompiledRuntimeContext", RuntimeContext)
    monkeypatch.setattr(workflow_module, "execute_node", execute_node)
    monkeypatch.setattr(
        workflow_cleanup_module,
        "refresh_node_workspace_descriptor",
        refresh_node_workspace_descriptor,
    )
    monkeypatch.setattr(
        workflow_cleanup_module,
        "cleanup_plan_workspace_refs",
        cleanup_refs,
    )

    asyncio.run(
        workflow_module.execute_workflow(
            _single_node_plan(output),
            output,
            invoker=object(),
            secret_context=SecretContext(),
            suppress_progress_output=True,
        )
    )

    assert refreshed == ["input"]
    assert ref_cleanup_thread_ids
    assert generated_cleanup_thread_ids
    assert worktree_cleanup_thread_ids
    assert refresh_thread_ids
    assert all(thread_id != main_thread_id for thread_id in ref_cleanup_thread_ids)
    assert all(
        thread_id != main_thread_id for thread_id in generated_cleanup_thread_ids
    )
    assert all(thread_id != main_thread_id for thread_id in worktree_cleanup_thread_ids)
    assert all(thread_id != main_thread_id for thread_id in refresh_thread_ids)


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

        def validate_execution_contract(self) -> None:
            return None

        def max_concurrent_nodes(self) -> int | None:
            return None

        def max_parallel_invocations(self) -> int | None:
            return None

    def cleanup_refs(plan: PreflightExecutionPlan) -> int:
        del plan
        return 0

    monkeypatch.setattr(workflow_module, "CompiledRuntimeContext", RuntimeContext)
    monkeypatch.setattr(
        workflow_cleanup_module,
        "cleanup_plan_workspace_refs",
        cleanup_refs,
    )

    asyncio.run(
        workflow_module.execute_workflow(
            _empty_plan(output),
            output,
            invoker=object(),
            secret_context=SecretContext(),
            event_sink=events.append,
            suppress_progress_output=True,
        )
    )

    assert drained_timeouts == [
        workflow_module.DEFERRED_WORKSPACE_CLEANUP_DRAIN_TIMEOUT_SECONDS
    ]
    cleanup_warnings = [
        event
        for event in events
        if event.event_type == "runtime_log"
        and event.payload.operation == "workspace_preparation_cancellation_cleanup"
    ]
    assert len(cleanup_warnings) == 1
    assert cleanup_warnings[0].payload.level == "warning"
    assert (
        "deferred cleanup failed"
        in cleanup_warnings[0].payload.attributes["first_error"]
    )
    assert events[-1].event_type == "workflow_finished"


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

    def ignore_successful_node_state(*args: object) -> None:
        del args

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

    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.md"),
    )
    runtime_context = SimpleNamespace(
        plan=_single_node_plan(output),
        generated_file_workspaces=registry,
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


def _empty_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        project_root=output.base_dir.as_posix(),
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime.now(UTC).isoformat(),
        workflow_name="workflow",
        workflow_signature="workflow-signature",
        execution_order=[],
        nodes=[],
        render_plans=[],
        static_resources=[],
        workspace_file_locators=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature="runtime-signature",
        fingerprint_metadata={"payload_version": "1"},
    )


def _single_node_plan(output: OutputManager) -> PreflightExecutionPlan:
    plan = _empty_plan(output)
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.md"),
    )
    return plan.model_copy(
        update={
            "execution_order": ["input"],
            "nodes": [node],
        }
    )
