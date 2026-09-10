import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.version import SCHEMA_VERSION


class InvocationLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_context_applies_child_environment_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            observed_child_environment: list[ChildProcessEnvironment | None] = []
            observed_contexts: list[InvocationContext | None] = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,
            ) -> CommandResult:
                observed_contexts.append(invocation_context)
                observed_child_environment.append(child_environment)
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            source = InvocationSourceContext(
                source_kind="project",
                source_node_id=None,
                source_commit="a" * 40,
                source_tree="b" * 40,
            )
            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                workspace=InvocationWorkspaceContext(
                    workspace_kind="snapshot",
                    materialization="snapshot_checkout",
                    logical_worktree_name="primary",
                    cwd=tmp_path,
                    invocation_source=source,
                    worktree_contract=InvocationWorktreeContract(
                        mode="blob_exact",
                        schema_version=SCHEMA_VERSION,
                    ),
                    child_environment_required=True,
                ),
            )
            config = AgentConfig(cli_cmd=[sys.executable], default_model="test")

            with patch.dict(
                "os.environ",
                {
                    "GIT_CONFIG_KEY_0": "core.fsmonitor",
                    "GIT_CONFIG_VALUE_0": "true",
                },
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            child_environment = observed_child_environment[0]
            invocation_context = observed_contexts[0]
            assert child_environment is not None
            assert child_environment is not None
            assert child_environment.set["GIT_CONFIG_NOSYSTEM"] == "1"
            assert child_environment.set["GIT_CONFIG_GLOBAL"] == os.devnull
            assert (
                child_environment.set["GIT_CEILING_DIRECTORIES"]
                == tmp_path.resolve().parent.as_posix()
            )
            assert "GIT_DIR" in child_environment.unset
            assert "GIT_CONFIG_KEY_0" in child_environment.unset
            assert "GIT_CONFIG_VALUE_0" in child_environment.unset
            assert invocation_context is not None
            assert invocation_context is not None
            assert invocation_context.workspace is not None
            assert invocation_context.workspace is not None
            assert not invocation_context.workspace.child_environment_applied

    async def test_workspace_child_environment_uses_checkout_root_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkout_root = Path(tmp_dir) / "checkout"
            cwd = checkout_root / "nested" / "project"
            cwd.mkdir(parents=True)
            output_file = Path(tmp_dir) / "output.txt"
            observed_child_environment: list[ChildProcessEnvironment | None] = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,
            ) -> CommandResult:
                observed_child_environment.append(child_environment)
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                workspace=InvocationWorkspaceContext(
                    workspace_kind="worktree",
                    materialization="worktree_checkout",
                    logical_worktree_name="primary",
                    cwd=cwd,
                    invocation_source=InvocationSourceContext(
                        source_kind="project",
                        source_node_id=None,
                        source_commit="a" * 40,
                        source_tree="b" * 40,
                    ),
                    worktree_contract=InvocationWorktreeContract(
                        mode="blob_exact",
                        schema_version=SCHEMA_VERSION,
                    ),
                    checkout_root=checkout_root,
                    writable=True,
                    child_environment_required=True,
                ),
            )

            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=[sys.executable], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                cwd=cwd,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            child_environment = observed_child_environment[0]
            assert child_environment is not None
            assert (
                child_environment.set["GIT_CEILING_DIRECTORIES"]
                == checkout_root.resolve().parent.as_posix()
            )

    async def test_workspace_retry_reset_runs_before_next_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            workspace_file = tmp_path / "attempt.txt"
            attempts = 0
            reset_calls = 0
            second_attempt_saw_reset = False

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                nonlocal attempts, second_attempt_saw_reset
                attempts += 1
                if attempts == 1:
                    workspace_file.write_text("dirty", encoding="utf-8")
                    return CommandResult(
                        returncode=2,
                        stdout_text="retry",
                        stderr_text="",
                    )
                second_attempt_saw_reset = not workspace_file.exists()
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            def reset_workspace() -> None:
                nonlocal reset_calls
                reset_calls += 1
                workspace_file.unlink(missing_ok=True)

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                retry_reset=reset_workspace,
            )

            await invoke_agent_with_runner(
                config=AgentConfig(
                    cli_cmd=[sys.executable],
                    default_model="test",
                    max_retries=1,
                    retry_delay_seconds=0,
                    retry_on_exit_codes=[2],
                ),
                model="test",
                prompt="prompt",
                output_file=output_file,
                cwd=tmp_path,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            assert attempts == 2
            assert reset_calls == 1
            assert second_attempt_saw_reset

    async def test_cancellation_during_retry_reset_waits_for_reset_to_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            reset_started = Event()
            reset_release = Event()

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                return CommandResult(returncode=2, stdout_text="retry", stderr_text="")

            def reset_workspace() -> None:
                reset_started.set()
                assert reset_release.wait(2)

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                retry_reset=reset_workspace,
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
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )
            )
            assert await asyncio.to_thread(reset_started.wait, 2)

            task.cancel()
            await asyncio.sleep(0.05)
            assert not task.done()
            reset_release.set()

            with pytest.raises(asyncio.CancelledError):
                await task
            assert not output_file.exists()
