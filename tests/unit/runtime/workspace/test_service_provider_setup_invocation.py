from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from time import monotonic

import pytest

import crewplane.runtime.workspace.service.worktree as workspace_service_worktree
import crewplane.runtime.workspace.service.worktree_failures as workspace_service_worktree_failures
from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import CommandResult, InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.preflight.models import (
    PreflightExecutionPlan,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.execution.provider_call import (
    ProviderCallDisplay,
    ProviderCallRequest,
    run_provider_call,
)
from crewplane.runtime.execution.runtime_context import CompiledRuntimeContext
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.setup import WorkspaceSetupError
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
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


def test_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    asyncio.run(
        _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
            tmp_path
        )
    )


def test_worktree_retry_reset_reruns_selected_setup(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
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
    output.create_stage_dir("implement")
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
        trusted_state = read_json_object(prepared.state_path)
        mutated_state = json.loads(json.dumps(trusted_state))
        mutated_state["run_id"] = "provider-mutated-run"
        prepared.state_path.write_text(json.dumps(mutated_state), encoding="utf-8")

        prepared.invocation_context.retry_reset()

        assert marker.read_text(encoding="utf-8") == "ready"
        assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
        prepared.mark_succeeded()
        state = read_json_object(prepared.state_path)
        assert state["status"] == "succeeded"
        assert state["run_id"] == trusted_state["run_id"]
        assert state["setup"]["status"] == "succeeded"
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_worktree_retry_reset_cancellation_terminates_retry_setup(tmp_path))


def test_worktree_setup_failure_records_retained_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(7)"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")

    def fail_cleanup(source: object, workspace_path: Path) -> None:
        del source
        assert workspace_path.exists()
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        fail_cleanup,
    )

    with pytest.raises(WorkspaceSetupError):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == "setup_failed_cleanup_failed"


def test_worktree_preparation_failure_records_retained_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree")
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")

    def fail_snapshot_entries(path: Path) -> dict[str, str]:
        del path
        raise RuntimeError("snapshot entries failed")

    def fail_cleanup(source: object, workspace_path: Path) -> None:
        del source
        assert workspace_path.exists()
        raise RuntimeError("cleanup denied")

    monkeypatch.setattr(
        workspace_service_worktree,
        "snapshot_entries",
        fail_snapshot_entries,
    )
    monkeypatch.setattr(
        workspace_service_worktree_failures,
        "remove_worktree_workspace",
        fail_cleanup,
    )

    with pytest.raises(RuntimeError, match="snapshot entries failed"):
        prepare_invocation_workspace(
            workspace_invocation_request(plan, output),
            workspace_invocation_context(),
        )

    state = read_json_object(
        output.create_stage_dir("implement") / "workspace-state.json"
    )
    assert state["status"] == "failed"
    assert state["workspace"]["retention"] == "retained"
    assert state["workspace"]["retained_reason"] == (
        "preparation_failed_cleanup_failed"
    )


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
    plan = _plan_with_setup(
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
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
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


async def _run_worktree_retry_reset_cancellation_terminates_retry_setup(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    setup_counter = tmp_path / "setup-count.txt"
    retry_setup_started = tmp_path / "retry-setup-started.txt"
    leaked_child_marker = tmp_path / "retry-setup-child-survived.txt"
    child_script = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(leaked_child_marker)!r}).write_text('alive')"
    )
    setup_script = (
        "import pathlib, subprocess, sys, time; "
        f"counter = pathlib.Path({str(setup_counter)!r}); "
        "count = int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(count + 1)); "
        "pathlib.Path('setup-marker.txt').write_text('ready'); "
        "started = "
        f"pathlib.Path({str(retry_setup_started)!r}); "
        "subprocess.Popen([sys.executable, '-c', "
        f"{child_script!r}]) if count else None; "
        "started.write_text('started') if count else None; "
        "time.sleep(30) if count else None"
    )
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=False, kind="worktree"),
        [[sys.executable, "-c", setup_script]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    node_dir = output.create_stage_dir("implement")
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    source = plan.workspace_source
    assert source is not None

    async def runner(
        cmd: list[str],  # noqa: ARG001
        stdin_data: bytes | None,  # noqa: ARG001
        log_file: Path | None,  # noqa: ARG001
        append_log: bool,  # noqa: ARG001
        log_header: bytes | None,  # noqa: ARG001
        cwd: Path,  # noqa: ARG001
        invocation_context: InvocationContext | None,  # noqa: ARG001
        idle_timeout_seconds: float | None,  # noqa: ARG001
        child_environment: object | None = None,  # noqa: ARG001
    ):
        return CommandResult(
            returncode=2,
            stdout_text="retry",
            stderr_text="",
        )

    task = asyncio.create_task(
        invoke_agent_with_runner(
            config=AgentConfig(
                cli_cmd=[sys.executable],
                default_model="test",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_exit_codes=[2],
            ),
            model="test",
            prompt="prompt",
            output_file=node_dir / "alpha_round1.md",
            cwd=prepared.cwd,
            log_file=None,
            invocation_context=prepared.invocation_context,
            command_runner=runner,
            plan_builder=build_cli_invocation_plan,
        )
    )
    try:
        await _wait_for_path(retry_setup_started)
        cancelled_at = monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = monotonic() - cancelled_at

        await asyncio.sleep(1.2)
        state = read_json_object(prepared.state_path)
        assert elapsed < 1.5
        assert state["setup"]["status"] == "cancelled"
        assert not leaked_child_marker.exists()
    finally:
        if not task.done():
            task.cancel()
        remove_worktree_workspace(source, prepared.workspace_path)


async def _run_provider_invocation_setup_failure_prevents_provider_call(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", "import sys; sys.exit(7)"]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
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


async def _run_provider_invocation_setup_cancellation_terminates_setup_process_group(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-only")
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    setup_started = tmp_path / "setup-started.txt"
    leaked_child_marker = tmp_path / "setup-child-survived.txt"
    child_script = (
        "import pathlib, time; "
        "time.sleep(1.0); "
        f"pathlib.Path({str(leaked_child_marker)!r}).write_text('alive')"
    )
    parent_script = (
        "import pathlib, subprocess, sys, time; "
        f"pathlib.Path({str(setup_started)!r}).write_text('started'); "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    plan = _plan_with_setup(
        workspace_plan(repo, cache_root, cleanup_on_success=True, kind="worktree"),
        [[sys.executable, "-c", parent_script]],
    )
    output = workspace_output_manager(tmp_path, repo, log_cli_output=True)
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
    assert node_dir is not None
    invoker = SetupMarkerInvoker(expect_marker=True)
    output_file = node_dir / "alpha_round1.md"

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
                prompt="cancel setup",
                output_file=output_file,
                role_label=ProviderRole.EXECUTOR,
                invoker=invoker,
                telemetry=None,
            ),
            display=ProviderCallDisplay(telemetry=None),
        )
    )
    await _wait_for_path(setup_started)

    cancelled_at = monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = monotonic() - cancelled_at
    assert runtime_context.deferred_workspace_cleanups.tasks == set()
    errors = await runtime_context.deferred_workspace_cleanups.drain(2.0)

    await asyncio.sleep(1.2)
    state = read_json_object(node_dir / "workspace-state.json")
    assert errors == ()
    assert elapsed < 1.5
    assert invoker.calls == 0
    assert not output_file.exists()
    assert state["status"] == "cancelled"
    assert state["setup"]["status"] == "cancelled"
    assert state["workspace"]["retention"] == "deleted"
    assert not (
        cache_root
        / "workspaces"
        / "test-repo"
        / plan.run_key_name
        / "implement-alpha-round1"
    ).exists()
    assert not leaked_child_marker.exists()


async def _run_provider_invocation_skips_unselected_setup_profile_for_snapshot(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = _plan_with_available_setup_profile(
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
    output.create_stage_dir("implement")
    runtime_context = CompiledRuntimeContext(
        plan=plan,
        secret_context=SecretContext(),
    )
    node_dir = output.get_stage_dir("implement")
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


async def _wait_for_path(path: Path, timeout_seconds: float = 2.0) -> None:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {path.as_posix()}")


class SetupMarkerInvoker:
    def __init__(self, expect_marker: bool) -> None:
        self.expect_marker = expect_marker
        self.calls = 0

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
        del config, model, prompt, log_file, invocation_context
        self.calls += 1
        marker = cwd / "setup-marker.txt"
        assert marker.exists() is self.expect_marker
        if self.expect_marker:
            assert marker.read_text(encoding="utf-8") == "ready"
        output_file.write_text("provider completed\n", encoding="utf-8")

    def log_presentation_for(self, config: AgentConfig) -> None:
        del config
        return None


def _plan_with_setup(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    plan = _plan_with_available_setup_profile(plan, commands)
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    updated_policy = policy.model_copy(
        update={
            "setup": WorkspaceSetupRecord(
                profile_name="bootstrap",
                commands=[
                    WorkspaceSetupCommandRecord(argv=argv, command_index=index)
                    for index, argv in enumerate(commands)
                ],
            )
        }
    )
    return plan.model_copy(
        update={
            "nodes": [node.model_copy(update={"workspace_policy": updated_policy})],
        }
    )


def _plan_with_available_setup_profile(
    plan: PreflightExecutionPlan,
    commands: list[list[str]],
) -> PreflightExecutionPlan:
    runtime_snapshot = dict(plan.runtime_config_snapshot)
    workspace = dict(runtime_snapshot.get("workspace", {}))
    workspace["setup_profiles"] = {"bootstrap": {"run": commands}}
    runtime_snapshot["workspace"] = workspace
    return plan.model_copy(update={"runtime_config_snapshot": runtime_snapshot})
