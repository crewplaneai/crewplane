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
