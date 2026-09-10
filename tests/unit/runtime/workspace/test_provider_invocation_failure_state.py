from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

import crewplane.runtime.execution.provider_call.lifecycle as provider_invocation_lifecycle_module
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    EventType,
    InvocationContext,
)
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
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


def test_failed_provider_invocation_preserves_applied_child_environment(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_failed_provider_invocation_preserves_applied_child_environment(tmp_path)
    )


def test_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_failed_provider_invocation_preserves_applied_child_environment(
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
    invoker = FailingCommandRunnerInvoker()
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )

    with pytest.raises(InvocationFailureError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="fail after launch",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=telemetry,
            ),
            display=ProviderCallDisplay(telemetry=telemetry),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert state["status"] == "failed"
    assert state["child_process_environment"]["required"] is True
    assert state["child_process_environment"]["applied"] is True
    assert invoker.child_environment is not None
    assert invoker.invocation_context is not None
    assert invoker.invocation_context.workspace is not None
    assert invoker.invocation_context.workspace.child_environment_applied is False
    failed_events = [
        event for event in events if event.event_type == EventType.INVOCATION_FAILED
    ]
    assert len(failed_events) == 1
    workspace_failed_events = [
        event
        for event in events
        if event.event_type == EventType.WORKSPACE_CONTEXT_RECORDED
        and event.payload.status == "failed"
    ]
    assert len(workspace_failed_events) == 1
    assert (
        workspace_failed_events[0].payload.workspace_child_environment_applied is True
    )


async def _run_failed_provider_invocation_preserves_provider_error_when_mark_failed_fails(
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
        return FailingMarkFailedWorkspace(
            cwd=repo,
            invocation_context=invocation_context,
        )

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "prepare_workspace_with_cancellation",
        fake_prepare,
    )

    with pytest.raises(RuntimeError, match="provider boom") as exc_info:
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="fail",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=FailingRuntimeInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    assert exception_notes_contain(
        exc_info.value,
        "Workspace failure handling failed: workspace mark_failed boom",
    )


class FailingRuntimeInvoker:
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
        raise RuntimeError("provider boom")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


class FailingMarkFailedWorkspace(PreparedWorkspace):
    def mark_failed(
        self,
        message: str,
        child_environment_applied: bool | None = None,
    ) -> None:
        del message, child_environment_applied
        raise RuntimeError("workspace mark_failed boom")


class FailingCommandRunnerInvoker:
    def __init__(self) -> None:
        self.child_environment: ChildProcessEnvironment | None = None
        self.invocation_context: InvocationContext | None = None

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
        async def command_runner(
            cmd: list[str],
            stdin_data: bytes | None,
            log_file: Path | None,
            append_log: bool,
            log_header: bytes | None,
            cwd: Path,
            invocation_context: InvocationContext | None,
            idle_timeout_seconds: float | None,
            child_environment: ChildProcessEnvironment | None = None,
        ) -> CommandResult:
            del (
                cmd,
                stdin_data,
                log_file,
                append_log,
                log_header,
                cwd,
                idle_timeout_seconds,
            )
            self.child_environment = child_environment
            self.invocation_context = invocation_context
            if (
                invocation_context is not None
                and invocation_context.workspace_environment_applied_recorder
                is not None
            ):
                invocation_context.workspace_environment_applied_recorder()
            return CommandResult(
                returncode=2,
                stdout_text="",
                stderr_text="provider failed",
            )

        await invoke_agent_with_runner(
            config=config,
            model=model,
            prompt=prompt,
            output_file=output_file,
            cwd=cwd,
            log_file=log_file,
            invocation_context=invocation_context,
            command_runner=command_runner,
            plan_builder=build_cli_invocation_plan,
        )

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None
