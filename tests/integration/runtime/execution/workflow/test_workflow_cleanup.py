from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

import crewplane.runtime.execution.workflow.cleanup as workflow_cleanup_module
import crewplane.runtime.execution.workflow.execution_session as workflow_execution_session_module
import crewplane.runtime.execution.workflow.node as workflow_node_module
import crewplane.runtime.execution.workflow.orchestration as workflow_module
import crewplane.runtime.execution.workflow.postconditions as workflow_postconditions_module
import crewplane.runtime.execution.workflow.scheduling as workflow_scheduling_module
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
from crewplane.runtime.execution.deferred_cleanup import DeferredAsyncCleanupRegistry
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.workspace_files.generated import (
    GeneratedFileWorkspaceCleanupResult,
    GeneratedFileWorkspaceRegistry,
)
from crewplane.runtime.workspace.worktree import cache as worktree_cache_module
from crewplane.runtime.workspace.worktree.cache import (
    ReusableWorktreeCheckout,
    WorktreeReuseCache,
    WorktreeReuseCleanupResult,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from crewplane.version import SCHEMA_VERSION
from tests.helpers.resume import make_workspace_source_snapshot


def _recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


def test_stage_publications_retain_all_recovery_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    generated_file = tmp_path / "generated.bin"
    node_state_file = tmp_path / "node-state.json"
    result_file.write_bytes(b"result")
    findings_file.write_bytes(b"findings")
    generated_file.write_bytes(b"generated")
    node_state_file.write_bytes(b"state")
    publications = RuntimePublicationRegistry()

    def do_nothing(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def return_node_state(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        return node_state_file

    def return_finalize_result(
        *args: object,
        **kwargs: object,
    ) -> StageFinalizeResult:
        del args, kwargs
        return StageFinalizeResult(
            stage_name="node",
            result_file=result_file,
            findings_file=findings_file,
            included_outputs=(),
            skipped_empty_outputs=(),
            warnings=(),
            generated_files=(generated_file,),
        )

    monkeypatch.setattr(
        workflow_node_module,
        "execute_input_stage",
        do_nothing,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "emit_stage_finalize_logs",
        do_nothing,
    )
    monkeypatch.setattr(
        workflow_node_module,
        "write_successful_node_state",
        return_node_state,
    )
    monkeypatch.setattr(
        output,
        "finalize_node",
        return_finalize_result,
    )
    node = PreflightExecutionNode(
        id="input",
        mode="input",
        artifact_contract=ArtifactContract(
            stage_path="input",
            output_path="input-result.md",
            log_path="input/logs",
            result_path="input-result.md",
        ),
    )
    runtime_context = SimpleNamespace(
        plan=_single_node_plan(output),
        generated_file_workspaces=GeneratedFileWorkspaceRegistry(),
        runtime_publications=publications,
    )

    asyncio.run(
        workflow_node_module.execute_node(
            node,
            output,
            invoker=object(),
            runtime_context=runtime_context,
            telemetry=None,
            workflow_identity="workflow",
        )
    )

    assert _recovery_payload(publications, result_file) == b"result"
    assert _recovery_payload(publications, findings_file) == b"findings"
    assert _recovery_payload(publications, node_state_file) == b"state"
    assert _recovery_payload(publications, generated_file) == b"generated"
    publications.close()


def test_workflow_closes_runtime_publication_registry_when_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed_registries: list[RuntimePublicationRegistry] = []
    original_close = RuntimePublicationRegistry.close

    def record_close(registry: RuntimePublicationRegistry) -> None:
        original_close(registry)
        closed_registries.append(registry)

    def fail_cleanup(registry: GeneratedFileWorkspaceRegistry) -> None:
        del registry
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(RuntimePublicationRegistry, "close", record_close)
    monkeypatch.setattr(GeneratedFileWorkspaceRegistry, "cleanup_all", fail_cleanup)
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(RuntimeError, match="cleanup exploded"):
        asyncio.run(
            workflow_module.execute_workflow(
                _empty_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                suppress_progress_output=True,
            )
        )

    assert len(closed_registries) == 1
    with pytest.raises(RuntimeError, match="registry is closed"):
        closed_registries[0].publish(
            tmp_path / "late.md", (0, hashlib.sha256().hexdigest())
        )


@pytest.mark.parametrize("deferred_failure", [False, True])
def test_workflow_postcondition_errors_preserve_phase_order_before_registry_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deferred_failure: bool,
) -> None:
    calls: list[str] = []
    deferred_error = RuntimeError("deferred cleanup failed")
    ref_error = RuntimeError("ref cleanup failed")
    generated_error = RuntimeError("generated cleanup failed")
    worktree_error = RuntimeError("worktree cleanup failed")
    state_refresh_error = RuntimeError("state refresh failed")
    descriptor_refresh_error = RuntimeError("descriptor refresh failed")

    class DeferredWorkspaceCleanups:
        has_unfinished_protected_tasks = False

        async def drain(self, timeout_seconds: float) -> tuple[Exception, ...]:
            del timeout_seconds
            calls.append("deferred")
            return (deferred_error,) if deferred_failure else ()

    class GeneratedFileRegistry:
        def cleanup_all(self) -> GeneratedFileWorkspaceCleanupResult:
            calls.append("generated")
            return GeneratedFileWorkspaceCleanupResult((generated_error,), ("input",))

    class WorktreeReuseRegistry:
        def cleanup_all(self) -> WorktreeReuseCleanupResult:
            calls.append("worktree")
            return WorktreeReuseCleanupResult(
                (worktree_error,),
                (tmp_path / "workspace-state.json",),
            )

    class PublicationRegistry:
        def close(self) -> None:
            calls.append("close")

    async def fail_ref_cleanup(*args: object) -> None:
        del args
        calls.append("refs")
        raise ref_error

    async def fail_state_refresh(*args: object) -> tuple[tuple[str, Exception], ...]:
        del args
        calls.append("state-refresh")
        return (("input", state_refresh_error),)

    async def fail_descriptor_refresh(
        *args: object,
    ) -> tuple[tuple[str, Exception], ...]:
        del args
        calls.append("descriptor-refresh")
        return (("input", descriptor_refresh_error),)

    monkeypatch.setattr(
        workflow_postconditions_module,
        "cleanup_successful_workspace_run_refs",
        fail_ref_cleanup,
    )
    monkeypatch.setattr(
        workflow_postconditions_module,
        "refresh_workspace_node_manifests_for_state_paths",
        fail_state_refresh,
    )
    monkeypatch.setattr(
        workflow_postconditions_module,
        "refresh_workspace_node_manifests",
        fail_descriptor_refresh,
    )
    output = OutputManager("Workflow", base_dir=tmp_path)
    runtime_context = SimpleNamespace(
        plan=_single_node_plan(output),
        deferred_workspace_cleanups=DeferredWorkspaceCleanups(),
        generated_file_workspaces=GeneratedFileRegistry(),
        worktree_reuse_cache=WorktreeReuseRegistry(),
        runtime_publications=PublicationRegistry(),
    )
    session = SimpleNamespace(
        runtime_context=runtime_context,
        telemetry=None,
        state=SimpleNamespace(running={}, statuses={"input": "succeeded"}),
    )

    errors = asyncio.run(
        workflow_postconditions_module.collect_workflow_postconditions(
            session,
            output,
        )
    )

    if deferred_failure:
        assert errors == [
            deferred_error,
            ref_error,
            state_refresh_error,
            descriptor_refresh_error,
        ]
        assert calls == [
            "deferred",
            "refs",
            "state-refresh",
            "descriptor-refresh",
            "close",
        ]
    else:
        assert errors == [
            ref_error,
            generated_error,
            worktree_error,
            state_refresh_error,
            descriptor_refresh_error,
        ]
        assert calls == [
            "deferred",
            "refs",
            "generated",
            "worktree",
            "state-refresh",
            "descriptor-refresh",
            "close",
        ]


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
        plan=_empty_plan(OutputManager("Workflow", base_dir=tmp_path)),
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


def test_workflow_cleanup_waits_for_cancelled_node_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    node_finalization_started = Event()
    node_finalization_running = Event()
    release_node_finalization = Event()
    cleanup_overlaps: list[bool] = []
    original_cleanup_all = GeneratedFileWorkspaceRegistry.cleanup_all

    def cleanup_all(
        registry: GeneratedFileWorkspaceRegistry,
    ) -> GeneratedFileWorkspaceCleanupResult:
        assert node_finalization_started.wait(timeout=2)
        cleanup_overlaps.append(node_finalization_running.is_set())
        release_node_finalization.set()
        return original_cleanup_all(registry)

    async def run_workflow(output: OutputManager) -> None:
        slow_node_started = asyncio.Event()
        keep_slow_node_running = asyncio.Event()

        async def execute_parallel_stage(
            node: PreflightExecutionNode,
            *args: object,
            **kwargs: object,
        ) -> None:
            del args, kwargs
            if node.id == "fail":
                await slow_node_started.wait()
                raise RuntimeError("sibling failed")

            slow_node_started.set()
            try:
                await keep_slow_node_running.wait()
            finally:
                node_finalization_running.set()
                node_finalization_started.set()
                try:
                    await asyncio.to_thread(release_node_finalization.wait, 0.25)
                finally:
                    node_finalization_running.clear()

        monkeypatch.setattr(
            workflow_node_module,
            "execute_parallel_stage",
            execute_parallel_stage,
        )
        async with asyncio.timeout(3):
            await workflow_module.execute_workflow(
                _two_parallel_node_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                suppress_progress_output=True,
            )

    monkeypatch.setattr(GeneratedFileWorkspaceRegistry, "cleanup_all", cleanup_all)
    output = OutputManager("Workflow", base_dir=tmp_path)

    try:
        with pytest.raises(RuntimeError, match="sibling failed"):
            asyncio.run(run_workflow(output))
    finally:
        release_node_finalization.set()

    assert cleanup_overlaps == [False]
    assert not node_finalization_running.is_set()


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
            self.runtime_publications = RuntimePublicationRegistry()

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

    monkeypatch.setattr(
        workflow_execution_session_module,
        "CompiledRuntimeContext",
        RuntimeContext,
    )
    monkeypatch.setattr(workflow_scheduling_module, "execute_node", execute_node)
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


def test_workflow_publishes_descriptor_refresh_failure_before_closing_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    events: list[ExecutionEvent] = []
    registries: list[RuntimePublicationRegistry] = []

    class GeneratedFileRegistry:
        def cleanup_all(self) -> SimpleNamespace:
            return SimpleNamespace(errors=(), cleaned_node_ids=("input",))

    class WorktreeReuseCache:
        def cleanup_all(self) -> SimpleNamespace:
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
            self.runtime_publications = RuntimePublicationRegistry()
            registries.append(self.runtime_publications)

        def validate_execution_contract(self) -> None:
            return None

        def max_concurrent_nodes(self) -> int | None:
            return None

        def max_parallel_invocations(self) -> int | None:
            return None

    async def execute_node(*args: object, **kwargs: object) -> None:
        del args, kwargs

    def fail_descriptor_refresh(
        node: PreflightExecutionNode,
        plan: PreflightExecutionPlan,
        output_store: OutputManager,
    ) -> Path:
        del node, plan, output_store
        raise RuntimeError("descriptor refresh exploded")

    def cleanup_refs(plan: PreflightExecutionPlan) -> int:
        del plan
        return 0

    monkeypatch.setattr(
        workflow_execution_session_module,
        "CompiledRuntimeContext",
        RuntimeContext,
    )
    monkeypatch.setattr(workflow_scheduling_module, "execute_node", execute_node)
    monkeypatch.setattr(
        workflow_cleanup_module,
        "refresh_node_workspace_descriptor",
        fail_descriptor_refresh,
    )
    monkeypatch.setattr(
        workflow_cleanup_module,
        "cleanup_plan_workspace_refs",
        cleanup_refs,
    )

    with pytest.raises(RuntimeError, match="descriptor refresh exploded"):
        asyncio.run(
            workflow_module.execute_workflow(
                _single_node_plan(output),
                output,
                invoker=object(),
                secret_context=SecretContext(),
                event_sink=events.append,
                suppress_progress_output=True,
            )
        )

    refresh_warnings = [
        event
        for event in events
        if event.event_type == EventType.RUNTIME_LOG
        and event.payload.operation == "workspace_manifest_refresh"
    ]
    assert len(refresh_warnings) == 1
    assert "descriptor refresh exploded" in refresh_warnings[0].payload.message
    assert len(registries) == 1
    with pytest.raises(RuntimeError, match="registry is closed"):
        registries[0].publish(
            tmp_path / "late.md",
            (0, hashlib.sha256().hexdigest()),
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
                _empty_plan(output),
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


def test_workflow_finalization_does_not_remove_cancelled_reuse_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_workflow_finalization_without_cancelled_reuse_cleanup(
            monkeypatch,
            tmp_path,
        )
    )


async def _run_workflow_finalization_without_cancelled_reuse_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path / "artifacts")
    plan = _empty_plan(output)
    source = make_workspace_source_snapshot()
    workspace_path = tmp_path / "cache" / "workspace"
    checkout_root = workspace_path / "checkout"
    checkout_root.mkdir(parents=True)
    state_path = tmp_path / "workspace-state.json"
    entry = ReusableWorktreeCheckout(
        node_id="implement",
        logical_worktree_name="primary",
        workspace_path=workspace_path,
        checkout_root=checkout_root,
        cwd=checkout_root,
        git_dir=workspace_path / "git-dir",
        source_commit=source.run_base_commit,
        source_tree=source.source_tree,
        source=source,
        state_path=state_path,
        cleanup_on_success=True,
        repository_id=source.repository_id,
        run_key_name="run-key",
    )
    source_ref = WorktreeSourceRef(
        source_kind="project",
        source_node_id=None,
        source_commit=entry.source_commit,
        source_tree=entry.source_tree,
    )
    reuse_cache = WorktreeReuseCache()
    reuse_cache.store(entry)
    leased = reuse_cache.take(
        entry.logical_worktree_name,
        source_ref,
        entry.repository_id,
        entry.run_key_name,
    )
    assert leased is entry

    reuse_cache.discard_workspace(leased.workspace_path)

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stalled_cleanup(
        source_arg: object,
        cleanup_path: Path,
        expected_git_dir: Path,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del source_arg, cleanup_path, expected_git_dir, cancel_requested
        loop.call_soon_threadsafe(cleanup_started.set)
        assert asyncio.run_coroutine_threadsafe(
            release_cleanup.wait(),
            loop,
        ).result(timeout=2)

    monkeypatch.setattr(
        worktree_cache_module,
        "remove_worktree_workspace",
        stalled_cleanup,
    )

    class RuntimeContext:
        def __init__(
            self,
            plan: PreflightExecutionPlan,
            secret_context: SecretContext,
        ) -> None:
            del secret_context
            self.plan = plan
            self.generated_file_workspaces = GeneratedFileWorkspaceRegistry()
            self.worktree_reuse_cache = reuse_cache
            self.deferred_workspace_cleanups = DeferredAsyncCleanupRegistry()
            self.runtime_publications = RuntimePublicationRegistry()

        def validate_execution_contract(self) -> None:
            return None

        def max_concurrent_nodes(self) -> int | None:
            return None

        def max_parallel_invocations(self) -> int | None:
            return None

    monkeypatch.setattr(
        workflow_execution_session_module,
        "CompiledRuntimeContext",
        RuntimeContext,
    )
    workflow_task = asyncio.create_task(
        workflow_module.execute_workflow(
            plan,
            output,
            invoker=object(),
            secret_context=SecretContext(),
            suppress_progress_output=True,
        )
    )
    cleanup_waiter = asyncio.create_task(cleanup_started.wait())
    try:
        done, _ = await asyncio.wait(
            (workflow_task, cleanup_waiter),
            timeout=2,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cleanup_waiter in done:
            release_cleanup.set()
            await asyncio.gather(workflow_task, return_exceptions=True)
            pytest.fail("workflow finalization started retained checkout removal")
        assert workflow_task in done
        await workflow_task
    finally:
        release_cleanup.set()
        cleanup_waiter.cancel()
        await asyncio.gather(cleanup_waiter, return_exceptions=True)

    assert not cleanup_started.is_set()
    assert workspace_path.exists()


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
        plan=_single_node_plan(output),
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


def _empty_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
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


def _two_parallel_node_plan(output: OutputManager) -> PreflightExecutionPlan:
    plan = _empty_plan(output)
    nodes = [
        PreflightExecutionNode(
            id=node_id,
            mode="parallel",
            artifact_contract=ArtifactContract(output_path=f"{node_id}.md"),
        )
        for node_id in ("slow", "fail")
    ]
    return plan.model_copy(
        update={
            "execution_order": [node.id for node in nodes],
            "nodes": nodes,
        }
    )
