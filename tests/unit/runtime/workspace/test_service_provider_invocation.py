from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

import crewplane.runtime.execution.provider_call.artifact_capture as provider_invocation_artifact_capture_module
import crewplane.runtime.execution.provider_call.lifecycle as provider_invocation_lifecycle_module
from crewplane.adapters.invokers.mock import MockInvokerAdapter
from crewplane.architecture.contracts import (
    EventType,
    InvocationContext,
)
from crewplane.artifacts.generated_files.paths import (
    GENERATED_FILE_SNAPSHOT_METADATA_NAME,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
    run_provider_invocation,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    disabled_workspace_plan,
    read_json_object,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    SuccessfulRuntimeInvoker,
)


def test_provider_invocation_uses_snapshot_workspace_cwd(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_uses_snapshot_workspace_cwd(tmp_path))


def test_artifact_capture_wrapper_failure_fails_provider_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_artifact_capture_wrapper_failure_fails_provider_invocation(
            tmp_path,
            monkeypatch,
        )
    )


def test_workspace_finalization_failure_fails_provider_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_workspace_finalization_failure_fails_provider_invocation(
            tmp_path,
            monkeypatch,
        )
    )


def test_generated_file_capture_failure_disables_project_root_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_generated_file_capture_failure_disables_project_root_fallback(
            tmp_path,
            monkeypatch,
        )
    )


def test_overlapping_project_root_invocations_do_not_cross_attribute_ambient_files(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_overlapping_project_root_invocations(tmp_path))


async def _run_provider_invocation_uses_snapshot_workspace_cwd(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=True,
        launch_mode="mock_no_child_process",
        controlled_child_environment=False,
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    invoker = MockInvokerAdapter().create_invoker(
        Config(
            version=SCHEMA_VERSION,
            agents={"alpha": AgentConfig(cli_cmd=["mock"])},
        ),
        options={"output_mode": "echo"},
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    events = []

    def record_event(event) -> None:
        if event.event_type == EventType.INVOCATION_STARTED:
            assert event.context.log_file is not None
            assert Path(event.context.log_file).is_file()
        events.append(event)

    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=record_event,
        suppress_console_output=True,
    )

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="hello workspace",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=telemetry,
        ),
        display=ProviderCallDisplay(telemetry=telemetry),
    )

    state_path = node_dir / "workspace-state.json"
    state = read_json_object(state_path)
    assert state["status"] == "succeeded"
    assert state["invoker"]["launch_mode"] == "mock_no_child_process"
    assert state["invoker"]["controlled_child_environment"] is False
    assert state["child_process_environment"]["required"] is False
    assert state["workspace"]["retention"] == "pending_cleanup"
    runtime_context.generated_file_workspaces.cleanup_node("implement")
    state = read_json_object(state_path)
    assert state["workspace"]["retention"] == "deleted"

    log_path = output.get_node_log_file(
        node_artifact_request("implement"), "alpha", "alpha", None, 1
    )
    assert log_path is not None
    log_record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log_record["cwd"].startswith(cache_root.as_posix())
    assert log_record["workspace"]["workspace_kind"] == "snapshot"
    assert log_record["workspace"]["materialization"] == "snapshot_checkout"
    assert log_record["workspace"]["child_environment_required"] is False
    started_events = [
        event for event in events if event.event_type == EventType.INVOCATION_STARTED
    ]
    assert len(started_events) == 1
    workspace_events = [
        event
        for event in events
        if event.event_type == EventType.WORKSPACE_CONTEXT_RECORDED
    ]
    assert len(workspace_events) == 2
    workspace_payload = workspace_events[0].payload
    assert workspace_payload.status == "running"
    assert workspace_payload.workspace_kind == "snapshot"
    assert workspace_payload.workspace_materialization == "snapshot_checkout"
    assert workspace_payload.workspace_source_kind == "project"
    assert workspace_payload.worktree_contract_mode == "blob_exact"
    assert workspace_payload.workspace_child_environment_required is False


async def _run_artifact_capture_wrapper_failure_fails_provider_invocation(
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
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )

    async def fail_artifact_capture(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        baseline: object,
        metadata: object,
    ) -> None:
        del request, prepared_workspace, baseline, metadata
        raise RuntimeError("artifact capture wrapper failed")

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "capture_invocation_generated_files",
        fail_artifact_capture,
    )
    output_file = node_dir / "alpha_round1.md"

    result = await run_provider_invocation(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="done",
            output_file=output_file,
            role_label=ProviderRole.EXECUTOR,
            invoker=SuccessfulRuntimeInvoker(),
            telemetry=telemetry,
        ),
        capture_exception=True,
        display=ProviderCallDisplay(telemetry=telemetry),
    )

    assert output_file.read_text(encoding="utf-8") == "done\n"
    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "artifact capture wrapper failed"
    assert not any(
        event.event_type == EventType.INVOCATION_FINISHED for event in events
    )
    assert any(event.event_type == EventType.INVOCATION_FAILED for event in events)


async def _run_workspace_finalization_failure_fails_provider_invocation(
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
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )

    async def fail_workspace_finalization(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        child_environment_applied: bool | None,
        generated_file_workspace: Path | None,
    ) -> None:
        del request, prepared_workspace, child_environment_applied
        del generated_file_workspace
        raise RuntimeError("workspace finalization failed")

    monkeypatch.setattr(
        provider_invocation_lifecycle_module,
        "finalize_successful_workspace",
        fail_workspace_finalization,
    )
    output_file = node_dir / "alpha_round1.md"

    result = await run_provider_invocation(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="done",
            output_file=output_file,
            role_label=ProviderRole.EXECUTOR,
            invoker=SuccessfulRuntimeInvoker(),
            telemetry=telemetry,
        ),
        capture_exception=True,
        display=ProviderCallDisplay(telemetry=telemetry),
    )

    assert isinstance(result.error, RuntimeError)
    assert str(result.error) == "workspace finalization failed"
    assert not any(
        event.event_type == EventType.INVOCATION_FINISHED for event in events
    )
    assert any(event.event_type == EventType.INVOCATION_FAILED for event in events)


async def _run_generated_file_capture_failure_disables_project_root_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    generated_file = repo / "src" / "app.txt"
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("live project content\n", encoding="utf-8")
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        suppress_console_output=True,
    )

    async def fail_generated_file_capture(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        baseline: object,
    ) -> None:
        del request, prepared_workspace, baseline
        raise RuntimeError("generated-file capture failed")

    monkeypatch.setattr(
        provider_invocation_artifact_capture_module,
        "snapshot_invocation_generated_files_async",
        fail_generated_file_capture,
    )
    output_file = node_dir / "alpha_round1.md"
    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="done",
            output_file=output_file,
            role_label=ProviderRole.EXECUTOR,
            invoker=SuccessfulRuntimeInvoker(),
            telemetry=telemetry,
        ),
        display=ProviderCallDisplay(telemetry=telemetry),
    )
    output_file.write_text(
        "Updated `src/app.txt`.\n\n## Generated Files\n\n- `src/app.txt`\n",
        encoding="utf-8",
    )

    workspace_roots = runtime_context.generated_file_workspaces.roots_for_node(
        "implement"
    )
    assert workspace_roots[output_file.resolve(strict=False)] is None
    result = output.finalize_node(
        node_artifact_request("implement"),
        generated_file_workspace_roots=workspace_roots,
    )
    result_text = result.result_file.read_text(encoding="utf-8")

    assert result.generated_files == ()
    assert "(../../src/app.txt)" not in result_text


async def _run_overlapping_project_root_invocations(tmp_path: Path) -> None:
    repo = create_git_repo(tmp_path)
    plan = disabled_workspace_plan(repo)
    output = workspace_output_manager(tmp_path, repo)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name=plan.workflow_name,
        run_id=plan.run_id,
        event_sink=events.append,
        suppress_console_output=True,
    )
    coordinator = OverlapCoordinator()
    base_provider = plan.nodes[0].provider_records[0]
    outputs = {
        "alpha": node_dir / "alpha_round1.md",
        "beta": node_dir / "beta_round1.md",
    }

    async def invoke(provider: str) -> object:
        provider_record = base_provider.model_copy(
            update={"provider": provider, "task_id": provider}
        )
        return await run_provider_invocation(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=provider_record,
                task_id=provider,
                audit_round_num=None,
                round_num=1,
                prompt="done",
                output_file=outputs[provider],
                role_label=ProviderRole.EXECUTOR,
                invoker=ClaimingRuntimeInvoker(
                    repo / f"{provider}.txt",
                    coordinator,
                ),
                telemetry=telemetry,
            ),
            capture_exception=True,
            display=ProviderCallDisplay(telemetry=telemetry),
        )

    tasks = [
        asyncio.create_task(invoke("alpha")),
        asyncio.create_task(invoke("beta")),
    ]
    await asyncio.wait_for(coordinator.both_ready.wait(), timeout=5)
    ambient = repo / "ambient-large.bin"
    with ambient.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    coordinator.release.set()
    results = await asyncio.gather(*tasks)

    assert all(result.error is None for result in results)
    assert not any(event.event_type == EventType.INVOCATION_FAILED for event in events)
    assert {
        event.context.task_id
        for event in events
        if event.event_type == EventType.INVOCATION_FINISHED
    } == {"alpha", "beta"}
    snapshot_roots = runtime_context.generated_file_workspaces.roots_for_node(
        "implement"
    )
    for provider, output_file in outputs.items():
        snapshot_root = snapshot_roots[output_file.resolve(strict=False)]
        assert snapshot_root is not None
        assert (snapshot_root / f"{provider}.txt").read_text(
            encoding="utf-8"
        ) == f"{provider} output\n"
        other_provider = "beta" if provider == "alpha" else "alpha"
        assert not (snapshot_root / f"{other_provider}.txt").exists()
        assert not (snapshot_root / ambient.name).exists()
        assert (snapshot_root / GENERATED_FILE_SNAPSHOT_METADATA_NAME).is_file()
        assert (
            outputs[provider]
            .read_text(encoding="utf-8")
            .endswith(f"- `{provider}.txt`\n")
        )


class OverlapCoordinator:
    def __init__(self) -> None:
        self.ready_count = 0
        self.both_ready = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_for_ambient_writer(self) -> None:
        self.ready_count += 1
        if self.ready_count == 2:
            self.both_ready.set()
        await self.release.wait()


class ClaimingRuntimeInvoker:
    def __init__(self, claim: Path, coordinator: OverlapCoordinator) -> None:
        self.claim = claim
        self.coordinator = coordinator

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
        del config, model, prompt, cwd, log_file, invocation_context
        self.claim.write_text(f"{self.claim.stem} output\n", encoding="utf-8")
        output_file.write_text(
            f"done\n\n## Generated Files\n\n- `{self.claim.name}`\n",
            encoding="utf-8",
        )
        await self.coordinator.wait_for_ambient_writer()

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None
