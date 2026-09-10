from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest

import crewplane.runtime.execution.provider_call.lifecycle as provider_invocation_lifecycle_module
import crewplane.runtime.execution.provider_call.workspace as provider_invocation_workspace_module
from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
)
from crewplane.runtime.execution.provider_call.workspace import (
    prepare_workspace_with_cancellation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
    DeferredAsyncCleanupRegistry,
)
from crewplane.runtime.workspace import (
    WorkspaceInvocationRequest,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    exception_notes_contain,
)


def test_provider_invocation_cancellation_marks_workspace_state(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_cancellation_marks_workspace_state(tmp_path))


def test_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_preparation_cancellation_marks_workspace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_cancellation_marks_workspace_state(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_provider_invocation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )

    async def fake_prepare(
        workspace_request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
        cleanup_registry: DeferredAsyncCleanupRegistry,
    ) -> PreparedWorkspace:
        del workspace_request, cleanup_registry
        return FailingMarkCancelledWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare,
    )

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="cancel",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=CancelledInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    assert exception_notes_contain(
        exc_info.value,
        "Workspace cancellation handling failed: workspace mark_cancelled boom",
    )


async def _run_provider_invocation_cancellation_marks_workspace_state(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None

    with pytest.raises(asyncio.CancelledError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="cancel",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=CancelledInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["required"] is True
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert state["workspace"]["retained_reason"] is None
    assert not (
        cache_root
        / "snapshots"
        / "test-repo"
        / plan.run_key_name
        / "implement-alpha-round1"
    ).exists()


async def _run_workspace_preparation_cancellation_marks_workspace_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True)
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    state_path = (
        output.create_node_dir(node_artifact_request("implement"))
        / "workspace-state.json"
    )
    workspace_path = cache_root / "snapshots" / "test-repo" / "prep-cancel"
    workspace_path.mkdir(parents=True)
    started = Event()
    release = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        started.set()
        release.wait(timeout=5)
        state_path.write_text(
            json.dumps(
                {
                    "status": "running",
                    "workspace": {
                        "retention": "retained",
                        "retained_reason": None,
                    },
                    "child_process_environment": {
                        "required": True,
                        "applied": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return PreparedWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
            workspace_kind="snapshot",
            workspace_path=workspace_path,
            state_path=state_path,
        )

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = read_json_object(state_path)
    assert state["status"] == "cancelled"
    assert state["child_process_environment"]["applied"] is False
    assert state["workspace"]["retention"] == "deleted"
    assert not workspace_path.exists()


async def _run_workspace_preparation_cancellation_preserves_cancel_when_mark_cancelled_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    started = Event()
    release = Event()

    def fake_prepare(
        request: WorkspaceInvocationRequest,
        invocation_context: InvocationContext,
    ) -> PreparedWorkspace:
        del request
        started.set()
        release.wait(timeout=5)
        return FailingMarkCancelledWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

    monkeypatch.setattr(
        provider_invocation_workspace_module,
        "prepare_invocation_workspace",
        fake_prepare,
    )
    request = WorkspaceInvocationRequest(
        plan=plan,
        output=output,
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )
    invocation_context = InvocationContext(
        node_id="implement",
        task_id="alpha",
        provider="alpha",
        role=ProviderRole.EXECUTOR,
        audit_round_num=None,
        round_num=1,
        findings_enabled=False,
    )

    cleanup_registry = DeferredAsyncCleanupRegistry()
    task = asyncio.create_task(
        prepare_workspace_with_cancellation(
            request,
            invocation_context,
            cleanup_registry,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert exception_notes_contain(
        exc_info.value,
        "Workspace preparation cancellation handling failed: "
        "workspace mark_cancelled boom",
    )


class CancelledInvoker:
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        del config, model, prompt, output_file, cwd, log_file, invocation_context
        raise asyncio.CancelledError()

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


class FailingMarkCancelledWorkspace(PreparedWorkspace):
    def mark_cancelled(
        self,
        message: str,
        child_environment_applied: bool | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del message, child_environment_applied, cancel_requested
        raise RuntimeError("workspace mark_cancelled boom")
