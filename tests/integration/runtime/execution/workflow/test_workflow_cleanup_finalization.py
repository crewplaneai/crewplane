from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

import crewplane.runtime.execution.workflow.cleanup as workflow_cleanup_module
import crewplane.runtime.execution.workflow.execution_session as workflow_execution_session_module
import crewplane.runtime.execution.workflow.node as workflow_node_module
import crewplane.runtime.execution.workflow.orchestration as workflow_module
import crewplane.runtime.execution.workflow.scheduling as workflow_scheduling_module
from crewplane.architecture.contracts import EventType
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
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers.resume import make_workspace_source_snapshot
from tests.integration.runtime.execution.workflow.workflow_cleanup_support import (
    empty_cleanup_plan,
    single_node_cleanup_plan,
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
            single_node_cleanup_plan(output),
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
                single_node_cleanup_plan(output),
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
    plan = empty_cleanup_plan(output)
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


def _two_parallel_node_plan(output: OutputManager) -> PreflightExecutionPlan:
    plan = empty_cleanup_plan(output)
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
