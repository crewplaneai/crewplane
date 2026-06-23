import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

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
from crewplane.runtime.agent.failures import InvocationFailureError
from crewplane.runtime.agent.invocation.command import run_command_once
from crewplane.runtime.agent.invoker import invoke_agent_with_runner
from crewplane.runtime.agent.process import stream_capture
from crewplane.runtime.agent.usage import InvocationUsage, estimate_token_count
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
            self.assertIsNotNone(child_environment)
            assert child_environment is not None
            self.assertEqual(child_environment.set["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(child_environment.set["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(
                child_environment.set["GIT_CEILING_DIRECTORIES"],
                tmp_path.parent.as_posix(),
            )
            self.assertIn("GIT_DIR", child_environment.unset)
            self.assertIn("GIT_CONFIG_KEY_0", child_environment.unset)
            self.assertIn("GIT_CONFIG_VALUE_0", child_environment.unset)
            self.assertIsNotNone(invocation_context)
            assert invocation_context is not None
            self.assertIsNotNone(invocation_context.workspace)
            assert invocation_context.workspace is not None
            self.assertFalse(invocation_context.workspace.child_environment_applied)

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
            self.assertEqual(
                child_environment.set["GIT_CEILING_DIRECTORIES"],
                checkout_root.parent.as_posix(),
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

            self.assertEqual(attempts, 2)
            self.assertEqual(reset_calls, 1)
            self.assertTrue(second_attempt_saw_reset)

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
            self.assertTrue(await asyncio.to_thread(reset_started.wait, 2))

            task.cancel()
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            reset_release.set()

            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(output_file.exists())

    async def test_cancellation_during_command_cleans_structured_output_without_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            structured_output_path: Path | None = None

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                nonlocal structured_output_path
                structured_output_path = Path(
                    cmd[cmd.index("--output-last-message") + 1]
                )
                structured_output_path.write_text("partial", encoding="utf-8")
                raise asyncio.CancelledError

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
            )

            with self.assertRaises(asyncio.CancelledError):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            self.assertFalse(structured_output_path.exists())
            self.assertEqual(usages, [])
            self.assertFalse(output_file.exists())

    async def test_command_stream_capture_files_are_cleaned_between_attempts(
        self,
    ) -> None:
        created_paths: list[Path] = []
        original_mkstemp = stream_capture.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, raw_path = original_mkstemp(*args, **kwargs)
            created_paths.append(Path(raw_path))
            return fd, raw_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch(
                    "crewplane.runtime.agent.process.stream_capture.tempfile.mkstemp",
                    side_effect=recording_mkstemp,
                ),
                self.assertRaises(InvocationFailureError),
            ):
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=[sys.executable, "-c", "raise ValueError('fail')"],
                        default_model="test",
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=Path(tmp_dir) / "output.txt",
                    cwd=Path(tmp_dir),
                    log_file=Path(tmp_dir) / "agent.log",
                    invocation_context=None,
                    command_runner=run_command_once,
                    plan_builder=build_cli_invocation_plan,
                )

            for path in created_paths:
                self.assertFalse(path.exists())

    async def test_cancellation_during_retry_sleep_cleans_structured_output_without_usage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            usages = []
            structured_output_path: Path | None = None

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                nonlocal structured_output_path
                structured_output_path = Path(
                    cmd[cmd.index("--output-last-message") + 1]
                )
                structured_output_path.write_text("retry marker", encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            async def cancel_sleep(wait_seconds: float) -> None:  # noqa: ARG001
                raise asyncio.CancelledError

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )

            with (
                patch(
                    "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                    new=cancel_sleep,
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            assert structured_output_path is not None
            self.assertFalse(structured_output_path.exists())
            self.assertEqual(usages, [])
            self.assertFalse(output_file.exists())

    async def test_structured_output_file_is_precleared_before_every_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            attempts = 0
            observed_missing_before_write = []

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                nonlocal attempts
                attempts += 1
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                observed_missing_before_write.append(not output_path.exists())
                output_path.write_text(
                    "retry marker" if attempts == 1 else "final",
                    encoding="utf-8",
                )
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry marker"],
            )
            sleep_mock = AsyncMock()
            with patch(
                "crewplane.runtime.agent.invocation.loop.asyncio.sleep",
                sleep_mock,
            ):
                await invoke_agent_with_runner(
                    config=config,
                    model="gpt-5.4",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=tmp_path,
                    log_file=None,
                    invocation_context=None,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(attempts, 2)
            self.assertEqual(observed_missing_before_write, [True, True])
            self.assertEqual(output_file.read_text(encoding="utf-8"), "final")

    async def test_success_records_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)

    async def test_visible_output_streams_persisted_stdout_to_final_artifact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_file = tmp_path / "output.txt"
            stream_path = tmp_path / "captured-stdout.txt"
            output_text = "line 1\n" + ("x" * 10_000)
            stream_path.write_text(output_text, encoding="utf-8")
            usages: list[InvocationUsage] = []

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
                return CommandResult(
                    returncode=0,
                    stdout_text="x" * 32,
                    stderr_text="",
                    stdout_path=stream_path,
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            await invoke_agent_with_runner(
                config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                model="test",
                prompt="prompt",
                output_file=output_file,
                cwd=tmp_path,
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(output_file.read_text(encoding="utf-8"), output_text)
            self.assertFalse(stream_path.exists())
            self.assertEqual(len(usages), 1)
            self.assertEqual(
                usages[0].visible_estimate_tokens,
                estimate_token_count(len("prompt"))
                + estimate_token_count(len(output_text)),
            )

    async def test_structured_provider_retry_records_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages: list[InvocationUsage] = []

            async def runner(
                cmd: list[str],
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                output_path = Path(cmd[cmd.index("--output-last-message") + 1])
                output_text = "retry" if not append_log else "f"
                output_path.write_text(output_text, encoding="utf-8")
                return CommandResult(
                    returncode=0,
                    stdout_text='{"type":"response.completed","response":{}}',
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="codex_executor_0",
                provider="codex",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            config = AgentConfig(
                cli_cmd=["codex", "exec"],
                provider_kind="codex",
                default_model="gpt-5.4",
                prompt_transport="stdin",
                prompt_transport_arg="-",
                max_retries=1,
                retry_delay_seconds=0,
                retry_on_output_contains=["retry"],
            )

            await invoke_agent_with_runner(
                config=config,
                model="gpt-5.4",
                prompt="",
                output_file=output_file,
                cwd=Path(tmp_dir),
                log_file=None,
                invocation_context=context,
                command_runner=runner,
                plan_builder=build_cli_invocation_plan,
            )

            self.assertEqual(len(usages), 1)
            usage = usages[0]
            self.assertEqual(usage.visible_estimate_tokens, 2)
            self.assertEqual(output_file.read_text(encoding="utf-8"), "f")

    async def test_failure_records_usage_once_before_reraising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                return CommandResult(
                    returncode=1,
                    stdout_text="",
                    stderr_text="fatal",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="generic",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            with self.assertRaises(InvocationFailureError):
                await invoke_agent_with_runner(
                    config=AgentConfig(cli_cmd=["echo"], default_model="test"),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=Path(tmp_dir),
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)

    async def test_quota_failure_records_usage_once_before_reraising(self) -> None:
        quota_message = (
            "You have exhausted your capacity on this model. "
            "Your quota will reset after 6h."
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_file = Path(tmp_dir) / "output.txt"
            usages = []

            async def runner(
                cmd: list[str],  # noqa: ARG001
                stdin_data: bytes | None,  # noqa: ARG001
                log_file: Path | None,  # noqa: ARG001
                append_log: bool,  # noqa: ARG001
                log_header: bytes | None,  # noqa: ARG001
                cwd: Path,  # noqa: ARG001
                invocation_context: InvocationContext | None,  # noqa: ARG001
                idle_timeout_seconds: float | None,  # noqa: ARG001 - Required by callback or protocol signature.
                child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
            ) -> CommandResult:
                return CommandResult(
                    returncode=0,
                    stdout_text=quota_message,
                    stderr_text="",
                )

            context = InvocationContext(
                node_id="node.a",
                task_id="generic_executor_0",
                provider="gemini",
                role=ProviderRole.EXECUTOR,
                usage_recorder=usages.append,
            )
            with self.assertRaises(InvocationFailureError):
                await invoke_agent_with_runner(
                    config=AgentConfig(
                        cli_cmd=["gemini"],
                        provider_kind="gemini",
                        default_model="test",
                        model_arg=None,
                        quota_reached_retry_delay_seconds=0,
                        quota_reset_sleep_floor_seconds=5,
                    ),
                    model="test",
                    prompt="prompt",
                    output_file=output_file,
                    cwd=Path(tmp_dir),
                    log_file=None,
                    invocation_context=context,
                    command_runner=runner,
                    plan_builder=build_cli_invocation_plan,
                )

            self.assertEqual(len(usages), 1)
            self.assertEqual(
                usages[0].visible_estimate_tokens,
                estimate_token_count(len("prompt"))
                + estimate_token_count(len(quota_message)),
            )
            self.assertFalse(output_file.exists())
