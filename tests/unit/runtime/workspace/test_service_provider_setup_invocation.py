from __future__ import annotations

import asyncio
import json
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import crewplane.runtime.workspace.service.retry_reset as workspace_service_retry_reset
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
)
from crewplane.runtime.execution.runtime_context import (
    CompiledRuntimeContext,
)
from crewplane.runtime.workspace import (
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.setup import (
    WorkspaceSetupError,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)
from tests.unit.runtime.workspace.service_provider_setup_invocation_support import (
    SetupMarkerInvoker,
    plan_with_available_setup_profile,
    plan_with_setup,
)


def test_provider_invocation_runs_selected_worktree_setup_before_provider(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_runs_selected_worktree_setup_before_provider(tmp_path)
    )


def test_provider_invocation_setup_failure_prevents_provider_call(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_provider_invocation_setup_failure_prevents_provider_call(tmp_path))


def test_worktree_retry_reset_reruns_selected_setup(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree"),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('ready')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None

    try:
        marker = prepared.cwd / "setup-marker.txt"
        assert marker.read_text(encoding="utf-8") == "ready"
        marker.unlink()
        (prepared.cwd / "README.md").write_text("dirty\n", encoding="utf-8")
        prepared.invocation_context.retry_reset()

        assert marker.read_text(encoding="utf-8") == "ready"
        assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
        prepared.mark_succeeded()
        state = read_json_object(prepared.state_path)
        assert state["status"] == "succeeded"
        assert state["run_id"] == plan.run_id
        assert state["setup"]["status"] == "succeeded"
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_setup_rejects_identity_change_at_state_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = plan_with_setup(
        workspace_plan(
            repo,
            tmp_path / "cache",
            cleanup_on_success=False,
            kind="worktree",
        ),
        [[sys.executable, "-c", "pass"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    assert prepared.workspace_state_payload is not None
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    original_update = workspace_service_retry_reset.update_workspace_setup

    def tamper_before_update(
        state_path: Path,
        setup_summary: Mapping[str, object],
        base_payload: Mapping[str, object] | None = None,
    ) -> None:
        payload = read_json_object(state_path)
        payload["run_id"] = "tampered-between-check-and-write"
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        original_update(
            state_path,
            setup_summary,
            base_payload=base_payload,
        )

    monkeypatch.setattr(
        workspace_service_retry_reset,
        "update_workspace_setup",
        tamper_before_update,
    )

    try:
        with pytest.raises(RuntimeError, match="state identity changed"):
            prepared.invocation_context.retry_reset()
        assert prepared.workspace_state_payload["run_id"] == plan.run_id
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


def test_provider_invocation_skips_unselected_setup_profile_for_snapshot(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_skips_unselected_setup_profile_for_snapshot(tmp_path)
    )


async def _run_provider_invocation_runs_selected_worktree_setup_before_provider(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(
            repo,
            cache_root,
            cleanup_on_success=True,
            kind="worktree",
            launch_mode="mock_no_child_process",
            controlled_child_environment=False,
        ),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('ready')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="setup first",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
    )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 1
    assert state["status"] == "succeeded"
    assert state["setup"]["status"] == "succeeded"
    assert state["setup"]["profile_name"] == "bootstrap"
    assert (node_dir / "workspace-setup" / "setup.json").is_file()
    assert (node_dir / "workspace-setup" / "setup.log").is_file()
    runtime_context.generated_file_workspaces.cleanup_node("implement")


async def _run_provider_invocation_setup_failure_prevents_provider_call(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(7)"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)
    output_file = node_dir / "alpha_round1.md"

    with pytest.raises(WorkspaceSetupError):
        await run_provider_call(
            ProviderCallRequest(
                runtime_context=runtime_context,
                output=output,
                node_id="implement",
                provider=plan.nodes[0].provider_records[0],
                task_id="alpha",
                audit_round_num=None,
                round_num=1,
                prompt="setup failure",
                output_file=output_file,
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 0
    assert not output_file.exists()
    assert state["status"] == "failed"
    assert state["setup"]["status"] == "failed"
    assert state["setup"]["commands"][0]["exit_code"] == 7


async def _run_provider_invocation_skips_unselected_setup_profile_for_snapshot(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = plan_with_available_setup_profile(
        workspace_plan(
            repo,
            cache_root,
            cleanup_on_success=True,
            kind="snapshot",
            launch_mode="mock_no_child_process",
            controlled_child_environment=False,
        ),
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('setup-marker.txt').write_text('unexpected')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_node_dir(node_artifact_request("implement"))
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_node_dir(node_artifact_request("implement"))
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=False)

    await run_provider_call(
        ProviderCallRequest(
            runtime_context=runtime_context,
            output=output,
            node_id="implement",
            provider=plan.nodes[0].provider_records[0],
            task_id="alpha",
            audit_round_num=None,
            round_num=1,
            prompt="snapshot setup skip",
            output_file=node_dir / "alpha_round1.md",
            role_label=ProviderRole.EXECUTOR,
            invoker=invoker,
            telemetry=None,
        ),
        display=ProviderCallDisplay(telemetry=None),
    )

    state = read_json_object(node_dir / "workspace-state.json")
    assert invoker.calls == 1
    assert "setup" not in state
    assert not (node_dir / "workspace-setup").exists()
    runtime_context.generated_file_workspaces.cleanup_node("implement")
