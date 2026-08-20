import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.config import AgentConfig
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    ProviderRecord,
)
from crewplane.core.preflight.runtime_config import (
    RuntimeAgentConfigSnapshot,
    runtime_agent_signature_payload,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.preflight.signatures import signature_for_payload
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import ExecutionEvent
from crewplane.runtime.agent.usage import estimate_token_count
from crewplane.runtime.execution.common import (
    CompiledRuntimeContext,
    ExecutionTelemetry,
    ProviderCallRequest,
    run_provider_call,
)
from crewplane.runtime.execution.provider_call import (
    provider_output as provider_call_output,
)
from crewplane.runtime.execution.provider_call import publish_invocation_output
from crewplane.runtime.execution.publication_registry import (
    RuntimePublicationRegistry,
)
from crewplane.runtime.execution.runtime_context import (
    DeferredAsyncCleanupRegistry,
    GeneratedFileWorkspaceRegistry,
)
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request


def _recovery_payload(
    registry: RuntimePublicationRegistry,
    path: Path,
) -> bytes | None:
    destination = BytesIO()
    if not registry.copy_recovery_payload_to(path, destination):
        return None
    return destination.getvalue()


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
        role=ProviderRole.EXECUTOR,
        model=model,
        task_id=f"{provider}_executor_0",
        agent_config_key=selected_key,
        invoker_alias="mock",
        agent_config_signature=_agent_signature(selected_key, selected_config, model),
        invoker_config_signature=_invoker_signature(),
    )


def _runtime_context(
    agent_configs: dict[str, AgentConfig],
) -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=PreflightExecutionPlan(
            plan_schema_version=SCHEMA_VERSION,
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


def test_invocation_output_publication_stages_on_destination_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "private"
    destination_dir = tmp_path / "node"
    source_dir.mkdir()
    destination_dir.mkdir()
    source = source_dir / "provider-output.md"
    destination = destination_dir / "reviewer.md"
    source.write_text("trusted reviewer output", encoding="utf-8")
    expected_signature = (
        23,
        "8073e76391e728426a7dfcd9c08af6dad50d89e69bdb982390c3f726fa995b31",
    )
    original_replace = provider_call_output.replace_contained_file
    staged_sources: list[Path] = []

    def record_replace(root: Path, relative_path: str, staged_source: Path) -> Path:
        staged_sources.append(staged_source)
        assert staged_source.parent == destination_dir
        return original_replace(root, relative_path, staged_source)

    monkeypatch.setattr(
        provider_call_output,
        "replace_contained_file",
        record_replace,
    )

    publications = RuntimePublicationRegistry()
    signature = publish_invocation_output(
        source,
        destination,
        publications,
        expected_signature,
    )

    assert signature == expected_signature
    assert destination.read_text(encoding="utf-8") == "trusted reviewer output"
    assert source.exists()
    assert len(staged_sources) == 1
    assert not staged_sources[0].exists()
    assert _recovery_payload(publications, destination) == b"trusted reviewer output"
    publications.close()


def test_runtime_publication_registry_validates_optional_recovery_source(
    tmp_path: Path,
) -> None:
    registry = RuntimePublicationRegistry()
    publication_path = tmp_path / "result.md"
    recovery_source = tmp_path / "trusted-result.md"
    payload = b"trusted result"
    signature = (len(payload), hashlib.sha256(payload).hexdigest())
    recovery_source.write_bytes(payload)

    registry.publish(publication_path, signature, recovery_source=recovery_source)

    assert _recovery_payload(registry, publication_path) == payload
    recovery_source.write_bytes(b"forged")
    with pytest.raises(ValueError, match="recovery source does not match"):
        registry.publish(
            publication_path,
            signature,
            recovery_source=recovery_source,
        )
    assert _recovery_payload(registry, publication_path) == payload

    second_path = tmp_path / "findings.md"
    second_source = tmp_path / "trusted-findings.md"
    second_payload = b"trusted findings"
    second_source.write_bytes(second_payload)
    second_signature = (
        len(second_payload),
        hashlib.sha256(second_payload).hexdigest(),
    )
    registry.publish(second_path, second_signature, recovery_source=second_source)
    replacement_payload = b"updated trusted result"
    recovery_source.write_bytes(replacement_payload)
    replacement_signature = (
        len(replacement_payload),
        hashlib.sha256(replacement_payload).hexdigest(),
    )
    registry.publish(
        publication_path,
        replacement_signature,
        recovery_source=recovery_source,
    )

    assert _recovery_payload(registry, publication_path) == replacement_payload
    assert _recovery_payload(registry, second_path) == second_payload

    registry.publish(publication_path, replacement_signature)

    assert _recovery_payload(registry, publication_path) is None
    registry.close()


def test_recovery_snapshot_does_not_register_or_duplicate_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temporary_file = tempfile.TemporaryFile
    recovery_files: list[BinaryIO] = []

    def record_temporary_file(mode: str = "w+b") -> BinaryIO:
        recovery_file = cast(BinaryIO, real_temporary_file(mode=mode))
        recovery_files.append(recovery_file)
        return recovery_file

    monkeypatch.setattr(tempfile, "TemporaryFile", record_temporary_file)
    source = tmp_path / "baseline.md"
    source.write_bytes(b"baseline")
    signature = file_size_and_sha256(source)
    registry = RuntimePublicationRegistry()

    registry.capture_recovery_snapshot(source, signature)
    registry.capture_recovery_snapshot(source, signature)

    assert registry.snapshot() == ({}, 0)
    assert len(recovery_files) == 1
    assert os.fstat(recovery_files[0].fileno()).st_size == len(b"baseline")
    assert _recovery_payload(registry, source) == b"baseline"
    registry.close()


def test_large_runtime_recovery_is_disk_backed_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_temporary_file = tempfile.TemporaryFile
    recovery_files: list[BinaryIO] = []

    def record_temporary_file(mode: str = "w+b") -> BinaryIO:
        recovery_file = cast(BinaryIO, real_temporary_file(mode=mode))
        recovery_files.append(recovery_file)
        return recovery_file

    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        record_temporary_file,
    )
    source = tmp_path / "large-result.md"
    payload_size = 16 * 1024 * 1024
    with source.open("wb") as source_file:
        chunk = b"x" * 1024 * 1024
        remaining_bytes = payload_size
        while remaining_bytes:
            source_file.write(chunk)
            remaining_bytes -= len(chunk)
    signature = file_size_and_sha256(source)
    registry = RuntimePublicationRegistry()

    registry.publish(source, signature, recovery_source=source)

    assert len(recovery_files) == 1
    assert os.fstat(recovery_files[0].fileno()).st_size == payload_size
    assert not recovery_files[0].closed
    source.write_bytes(b"forged replacement")
    with pytest.raises(ValueError, match="recovery source does not match"):
        registry.publish(source, signature, recovery_source=source)
    assert os.fstat(recovery_files[0].fileno()).st_size == payload_size
    source.unlink()
    restored = tmp_path / "restored-large-result.md"
    with restored.open("wb") as destination:
        assert registry.copy_recovery_payload_to(source, destination)
    assert file_size_and_sha256(restored) == signature

    registry.close()
    registry.close()

    assert recovery_files[0].closed


def test_runtime_recovery_descriptor_count_is_bounded() -> None:
    script = textwrap.dedent(
        """
        import hashlib
        import resource
        import tempfile
        from pathlib import Path

        from crewplane.runtime.execution.publication_registry import (
            RuntimePublicationRegistry,
        )

        _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        soft_limit = (
            64
            if hard_limit == resource.RLIM_INFINITY
            else min(64, hard_limit)
        )
        if soft_limit < 32:
            raise RuntimeError("Descriptor limit is too low for this regression test.")
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, hard_limit))
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.bin"
            source.write_bytes(b"x")
            signature = (1, hashlib.sha256(b"x").hexdigest())
            registry = RuntimePublicationRegistry()
            for index in range(128):
                registry.publish(
                    root_path / f"publication-{index}.bin",
                    signature,
                    recovery_source=source,
                )
            registry.close()
        print(index + 1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "128"


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


def _agent_signature(
    agent_config_key: str,
    agent_config: AgentConfig,
    resolved_model: str | None,
) -> str:
    agent_snapshot = RuntimeAgentConfigSnapshot.model_validate(
        agent_config.model_dump(mode="json", exclude_none=True)
    )
    return signature_for_payload(
        runtime_agent_signature_payload(
            agent_config_key,
            agent_snapshot,
            resolved_model,
        )
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


class _PartialOutputFailingInvoker(_FailingInvoker):
    def __init__(self, partial_output: str) -> None:
        self.partial_output = partial_output

    async def invoke(  # type: ignore[no-untyped-def]
        self,
        config,  # noqa: ARG002 - Required by protocol.
        model,  # noqa: ARG002 - Required by protocol.
        prompt,  # noqa: ARG002 - Required by protocol.
        output_file,
        cwd,  # noqa: ARG002 - Required by protocol.
        log_file=None,  # noqa: ARG002 - Required by protocol.
        invocation_context=None,  # noqa: ARG002 - Required by protocol.
    ) -> None:
        output_file.write_text(self.partial_output, encoding="utf-8")
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
    node_dir = output.create_node_dir(node_artifact_request("node.a"))
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
        role_label=ProviderRole.EXECUTOR,
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


def test_failure_telemetry_counts_partial_private_provider_output(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_node_dir(node_artifact_request("node.a"))
    agent_config = AgentConfig(cli_cmd=["echo"], default_model="model-a")
    prompt = "provider prompt"
    partial_output = "partial provider output"
    events: list[ExecutionEvent] = []
    canonical_output = node_dir / "alpha_executor_0_round1.md"
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
        prompt=prompt,
        output_file=canonical_output,
        role_label=ProviderRole.EXECUTOR,
        invoker=_PartialOutputFailingInvoker(partial_output),
        telemetry=ExecutionTelemetry(
            workflow_name="workflow",
            run_id=output.run_id,
            event_sink=events.append,
            suppress_console_output=True,
        ),
    )

    with pytest.raises(RuntimeError, match="provider boom"):
        asyncio.run(run_provider_call(request))

    failed_event = next(
        event for event in events if event.event_type == "invocation_failed"
    )
    assert failed_event.payload.visible_estimate_tokens == (
        estimate_token_count(len(prompt)) + estimate_token_count(len(partial_output))
    )
    assert not canonical_output.exists()


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
    node_dir = output.create_node_dir(node_artifact_request("node.a"))
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
        role_label=ProviderRole.EXECUTOR,
        invoker=invoker,
        telemetry=None,
    )

    asyncio.run(run_provider_call(request))

    assert invoker.models == [None]


def test_provider_invocation_uses_compiled_agent_config_key(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node_dir = output.create_node_dir(node_artifact_request("node.a"))
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
        role_label=ProviderRole.EXECUTOR,
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
    node_dir = output.create_node_dir(node_artifact_request("node.a"))
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
        role_label=ProviderRole.EXECUTOR,
        invoker=_RecordingInvoker(),
        telemetry=None,
    )

    with pytest.raises(ValueError, match="agent config signature"):
        asyncio.run(run_provider_call(request))
