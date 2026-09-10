from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from threading import Event, Timer
from time import monotonic

import pytest

import crewplane.runtime.execution.provider_call.generated_files as provider_invocation_generated_files_module
from crewplane.architecture.contracts import (
    InvocationContext,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    record_generated_file_workspace,
    run_provider_call,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
)
from crewplane.runtime.workspace.prepared_workspace import PreparedWorkspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    disabled_workspace_plan,
    read_json_object,
    workspace_output_manager,
)
from tests.unit.runtime.workspace.service_provider_invocation_support import (
    SuccessfulRuntimeInvoker,
)


def test_generated_file_workspace_cleanup_registered_after_cwd_deleted(
    tmp_path: Path,
) -> None:
    plan = disabled_workspace_plan(tmp_path)
    output = workspace_output_manager(tmp_path, tmp_path)
    node_dir = output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    state_path = node_dir / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": "stage_finalization_pending",
                },
            }
        ),
        encoding="utf-8",
    )

    record_generated_file_workspace(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="done",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=object(),
            telemetry=None,
        ),
        PreparedWorkspace(
            cwd=workspace_path / "checkout",
            invocation_context=InvocationContext(
                node_id="implement",
                task_id="alpha",
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                audit_round_num=None,
                round_num=1,
                findings_enabled=False,
            ),
            workspace_path=workspace_path,
            state_path=state_path,
            cleanup_on_success=True,
        ),
        None,
    )

    assert runtime_context.generated_file_workspaces.roots_for_node("implement") == {}
    errors = runtime_context.generated_file_workspaces.cleanup_all_best_effort()
    assert errors == ()
    assert not workspace_path.exists()
    assert read_json_object(state_path)["workspace"]["retention"] == "deleted"


def test_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(
        _run_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
            tmp_path,
            monkeypatch,
        )
    )


async def _run_provider_invocation_generated_file_snapshot_does_not_block_event_loop(
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
    started = Event()
    release = Event()

    def blocking_snapshot(
        request: ProviderCallRequest,
        prepared_workspace: PreparedWorkspace,
        change_baseline: object | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        del request, prepared_workspace, change_baseline, cancel_requested
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(
        provider_invocation_generated_files_module,
        "snapshot_invocation_generated_files",
        blocking_snapshot,
    )

    fallback_release = Timer(1.0, release.set)
    fallback_release.start()
    started_at = monotonic()
    task = asyncio.create_task(
        run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="done",
                output_file=node_dir / "alpha_round1.md",
                role_label=ProviderRole.EXECUTOR,
                invoker=SuccessfulRuntimeInvoker(),
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
        assert monotonic() - started_at < 0.5
        release.set()
        await task
    finally:
        release.set()
        fallback_release.cancel()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
