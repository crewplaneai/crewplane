import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionPlan,
    ProviderRecord,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.core.preflight.signatures import signature_for_payload
from orchestrator_cli.observability.events import ExecutionEvent
from orchestrator_cli.runtime.execution.common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    ProviderCallRequest,
    run_provider_call,
)
from orchestrator_cli.runtime.execution.runtime_context import (
    DeferredAsyncCleanupRegistry,
    GeneratedFileWorkspaceRegistry,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _provider_record(
    provider: str = "alpha",
    model: str | None = None,
    agent_config_key: str | None = None,
    agent_config: AgentConfig | None = None,
) -> ProviderRecord:
    selected_key = agent_config_key or provider
    selected_config = agent_config or AgentConfig(
        cli_cmd=["echo"],
        default_model="model-a",
    )
    return ProviderRecord(
        provider=provider,
        role="executor",
        model=model,
        task_id=f"{provider}_executor_0",
        agent_config_key=selected_key,
        invoker_alias="mock",
        agent_config_signature=_agent_signature(selected_key, selected_config),
        invoker_config_signature=_invoker_signature(),
    )


def _runtime_context(
    agent_configs: dict[str, AgentConfig],
) -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            run_id="run",
            run_key_name="workflow-run",
            project_root="/tmp/project-root",
            context_root="/tmp/workflow-run",
            manifest_root="/tmp/workflow-run/manifests",
            created_at=datetime(2026, 6, 3).isoformat(),
            workflow_name="workflow",
            workflow_signature="0" * 64,
            execution_order=[],
            nodes=[],
            render_plans=[],
            static_resources=[],
            token_catalog=[],
            dependency_graph=[],
            runtime_config_snapshot={
                "agents": {
                    key: config.model_dump(mode="json", exclude_none=True)
                    for key, config in agent_configs.items()
                },
                "execution": {},
                "invoker": {**_invoker_payload(), "option_scopes": {}},
                "schema_version": SCHEMA_VERSION,
            },
            effective_runtime_config_signature="1" * 64,
            fingerprint_metadata={"payload_version": "1"},
        ),
        secret_context=SecretContext(),
    )


def test_deferred_cleanup_registry_drains_follow_up_tasks() -> None:
    async def run_test() -> tuple[tuple[Exception, ...], list[str], int]:
        registry = DeferredAsyncCleanupRegistry()
        completed: list[str] = []

        async def follow_up_cleanup() -> None:
            completed.append("follow-up")

        async def initial_cleanup() -> None:
            completed.append("initial")
            registry.register(follow_up_cleanup())

        registry.register(initial_cleanup())
        errors = await registry.drain(1.0)
        return errors, completed, len(registry.tasks)

    errors, completed, task_count = asyncio.run(run_test())

    assert errors == ()
    assert completed == ["initial", "follow-up"]
    assert task_count == 0


def test_deferred_cleanup_registry_cancels_follow_up_tasks_after_deadline() -> None:
    async def run_test() -> tuple[
        tuple[Exception, ...],
        tuple[Exception, ...],
        int,
        bool,
        bool,
    ]:
        registry = DeferredAsyncCleanupRegistry()
        release = asyncio.Event()
        completed = False
        cancelled = False

        async def slow_follow_up_cleanup() -> None:
            nonlocal cancelled, completed
            try:
                await release.wait()
                completed = True
            except asyncio.CancelledError:
                cancelled = True
                raise

        async def initial_cleanup() -> None:
            registry.register(slow_follow_up_cleanup())

        registry.register(initial_cleanup())
        timeout_errors = await registry.drain(0.01)
        remaining_task_count = len(registry.tasks)
        follow_up_errors = await registry.drain(1.0)
        return (
            timeout_errors,
            follow_up_errors,
            remaining_task_count,
            completed,
            cancelled,
        )

    timeout_errors, follow_up_errors, remaining_task_count, completed, cancelled = (
        asyncio.run(run_test())
    )

    assert any(isinstance(error, TimeoutError) for error in timeout_errors)
    assert follow_up_errors == ()
    assert remaining_task_count == 0
    assert completed is False
    assert cancelled is True


def test_generated_file_workspace_registry_cleans_pending_callbacks_best_effort(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node.a",
        tmp_path / "node.a.md",
        tmp_path / "workspace-a",
        lambda: cleaned.append("node.a"),
    )
    registry.record(
        "node.b",
        tmp_path / "node.b.md",
        tmp_path / "workspace-b",
        fail_cleanup,
    )

    errors = registry.cleanup_all_best_effort()

    assert cleaned == ["node.a"]
    assert len(errors) == 1
    assert registry.roots_for_node("node.a") == {}
    assert registry.roots_for_node("node.b") == {
        (tmp_path / "node.b.md").resolve(): (tmp_path / "workspace-b").resolve()
    }


def test_generated_file_workspace_registry_keeps_failed_cleanup_for_retry(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    attempts = 0
    cleaned: list[str] = []

    def flaky_cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary cleanup failure")
        cleaned.append("retry")

    registry.record(
        "node",
        tmp_path / "node.md",
        None,
        flaky_cleanup,
    )

    with pytest.raises(RuntimeError, match="Generated-file workspace cleanup failed"):
        registry.cleanup_node("node")

    assert registry.roots_for_node("node") == {}
    errors = registry.cleanup_all_best_effort()
    assert errors == ()
    assert cleaned == ["retry"]


def test_generated_file_workspace_registry_reports_cleaned_nodes(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        lambda: cleaned.append("node"),
    )

    result = registry.cleanup_all()

    assert result.errors == ()
    assert result.cleaned_node_ids == ("node",)
    assert cleaned == ["node"]
    assert registry.roots_for_node("node") == {}


def test_generated_file_workspace_registry_reports_partially_cleaned_nodes(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()
    cleaned: list[str] = []

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "first.md",
        tmp_path / "workspace-a",
        lambda: cleaned.append("first"),
    )
    registry.record(
        "node",
        tmp_path / "second.md",
        tmp_path / "workspace-b",
        fail_cleanup,
    )

    result = registry.cleanup_all()

    assert len(result.errors) == 1
    assert result.cleaned_node_ids == ("node",)
    assert cleaned == ["first"]
    assert registry.roots_for_node("node") == {
        (tmp_path / "first.md").resolve(): (tmp_path / "workspace-a").resolve(),
        (tmp_path / "second.md").resolve(): (tmp_path / "workspace-b").resolve(),
    }


def test_generated_file_workspace_registry_node_cleanup_can_be_best_effort(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        fail_cleanup,
    )

    errors = registry.cleanup_node_best_effort("node")

    assert len(errors) == 1
    assert registry.roots_for_node("node") == {
        (tmp_path / "node.md").resolve(): (tmp_path / "workspace").resolve()
    }


def test_generated_file_workspace_registry_node_cleanup_can_retain_workspace(
    tmp_path: Path,
) -> None:
    registry = GeneratedFileWorkspaceRegistry()

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    registry.record(
        "node",
        tmp_path / "node.md",
        tmp_path / "workspace",
        fail_cleanup,
    )

    errors = registry.cleanup_node_best_effort(
        "node",
        retain_failed_callbacks=False,
    )

    assert len(errors) == 1
    assert registry.roots_for_node("node") == {}
    assert registry.cleanup_all_best_effort() == ()


def _agent_signature(agent_config_key: str, agent_config: AgentConfig) -> str:
    return signature_for_payload(
        {
            "agent_config": agent_config.model_dump(mode="json", exclude_none=True),
            "agent_config_key": agent_config_key,
        }
    )


def _invoker_payload() -> dict[str, object]:
    return {
        "capabilities": {},
        "implementation": "mock",
        "options": {},
        "resolved_identity": "mock",
    }


def _invoker_signature() -> str:
    return signature_for_payload(_invoker_payload())


class _FailingInvoker:
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
        raise RuntimeError("provider boom")


def test_runtime_context_preserves_default_disabled_invocation_timeout() -> None:
    agent_config = AgentConfig(
        cli_cmd=["echo"],
    )
    runtime_context = _runtime_context({"alpha": agent_config})

    resolved_config = runtime_context.agent_config_for_provider(
        _provider_record("alpha", agent_config=agent_config),
    )

    assert resolved_config.invocation_timeout_seconds is None


def test_failure_telemetry_error_does_not_mask_provider_error(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_stage_dir("node.a")
    agent_config = AgentConfig(cli_cmd=["echo"], default_model="model-a")

    def event_sink(event: ExecutionEvent) -> None:
        if event.event_type == "invocation_failed":
            raise RuntimeError("sink boom")

    request = ProviderCallRequest(
        runtime_context=_runtime_context({"alpha": agent_config}),
        output=output,
        node_id="node.a",
        provider=_provider_record(
            "alpha",
            "compiled-model",
            agent_config=agent_config,
        ),
        task_id="alpha_executor_0",
        audit_round_num=None,
        round_num=1,
        prompt="prompt",
        output_file=node_dir / "alpha_executor_0_round1.md",
        role_label="executor",
        invoker=_FailingInvoker(),
        telemetry=ExecutionTelemetry(
            workflow_name="workflow",
            run_id=output.run_id,
            event_sink=event_sink,
            suppress_console_output=True,
        ),
    )

    with pytest.raises(RuntimeError, match="provider boom") as exc_info:
        asyncio.run(run_provider_call(request))

    assert any(
        "invocation failure telemetry failed" in note
        for note in getattr(exc_info.value, "__notes__", [])
    )


class _RecordingInvoker:
    def __init__(self) -> None:
        self.models: list[str | None] = []
        self.commands: list[list[str]] = []

    def log_presentation_for(self, config):  # type: ignore[no-untyped-def]  # noqa: ARG002 - Required by protocol.
        return None

    async def invoke(  # type: ignore[no-untyped-def]
        self,
        config,
        model,
        prompt,  # noqa: ARG002 - Required by protocol.
        output_file,
        cwd,  # noqa: ARG002 - Required by protocol.
        log_file=None,  # noqa: ARG002 - Required by protocol.
        invocation_context=None,  # noqa: ARG002 - Required by protocol.
    ) -> None:
        self.models.append(model)
        self.commands.append(config.get_command())
        output_file.write_text("ok", encoding="utf-8")


def test_provider_invocation_uses_compiled_provider_record_model(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_stage_dir("node.a")
    invoker = _RecordingInvoker()
    agent_config = AgentConfig(cli_cmd=["echo"], default_model="config-default-model")
    request = ProviderCallRequest(
        runtime_context=_runtime_context({"alpha": agent_config}),
        output=output,
        node_id="node.a",
        provider=_provider_record("alpha", None, agent_config=agent_config),
        task_id="alpha_executor_0",
        audit_round_num=None,
        round_num=1,
        prompt="prompt",
        output_file=node_dir / "alpha_executor_0_round1.md",
        role_label="executor",
        invoker=invoker,
        telemetry=None,
    )

    asyncio.run(run_provider_call(request))

    assert invoker.models == [None]


def test_provider_invocation_uses_compiled_agent_config_key(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_stage_dir("node.a")
    invoker = _RecordingInvoker()
    agent_config = AgentConfig(
        cli_cmd=["compiled-command"],
        default_model="config-default-model",
    )
    request = ProviderCallRequest(
        runtime_context=_runtime_context({"compiled-key": agent_config}),
        output=output,
        node_id="node.a",
        provider=_provider_record(
            provider="display-provider",
            model="compiled-model",
            agent_config_key="compiled-key",
            agent_config=agent_config,
        ),
        task_id="display_provider_executor_0",
        audit_round_num=None,
        round_num=1,
        prompt="prompt",
        output_file=node_dir / "display_provider_executor_0_round1.md",
        role_label="executor",
        invoker=invoker,
        telemetry=None,
    )

    asyncio.run(run_provider_call(request))

    assert invoker.models == ["compiled-model"]
    assert invoker.commands == [["compiled-command"]]


def test_provider_invocation_rejects_unsigned_agent_config_drift(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_stage_dir("node.a")
    signed_config = AgentConfig(cli_cmd=["signed-command"], default_model="model-a")
    drifted_config = AgentConfig(cli_cmd=["drifted-command"], default_model="model-a")
    request = ProviderCallRequest(
        runtime_context=_runtime_context({"alpha": drifted_config}),
        output=output,
        node_id="node.a",
        provider=_provider_record(
            "alpha", "compiled-model", agent_config=signed_config
        ),
        task_id="alpha_executor_0",
        audit_round_num=None,
        round_num=1,
        prompt="prompt",
        output_file=node_dir / "alpha_executor_0_round1.md",
        role_label="executor",
        invoker=_RecordingInvoker(),
        telemetry=None,
    )

    with pytest.raises(ValueError, match="agent config signature"):
        asyncio.run(run_provider_call(request))
