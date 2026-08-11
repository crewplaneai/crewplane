import asyncio
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
    InvocationProcessEvent,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import (
    build_invocation_runtime,
    cleanup_structured_output_file,
    prepare_runtime_for_attempt,
    run_command_once,
    run_invocation_attempt,
)
from crewplane.runtime.agent.process.stream_capture import ProcessOutputCapture
from crewplane.runtime.agent.workspace_environment import workspace_child_environment
from crewplane.version import SCHEMA_VERSION


def test_prepare_runtime_for_attempt_clears_stale_structured_output() -> None:
    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            default_model="gpt-5.5",
            prompt_transport_arg="-",
        ),
        "gpt-5.5",
        "prompt",
        Path("output.txt"),
    )
    runtime = build_invocation_runtime(plan)
    assert runtime.structured_output_file is not None
    try:
        runtime.structured_output_file.write_text("stale", encoding="utf-8")

        prepare_runtime_for_attempt(runtime)

        assert not runtime.structured_output_file.exists()
    finally:
        cleanup_structured_output_file(runtime.structured_output_file)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_command_once_emits_started_and_exited_process_events(
        self,
    ) -> None:
        events: list[InvocationProcessEvent] = []
        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=events.append,
        )

        result = await run_command_once(
            cmd=[sys.executable, "-c", "print('ok')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=Path.cwd(),
            invocation_context=context,
            idle_timeout_seconds=None,
        )
        try:
            self.assertEqual([event.status for event in events], ["started", "exited"])
            self.assertEqual(events[0].pid, events[1].pid)
            self.assertEqual([event.attempt for event in events], [1, 1])
            self.assertIsNone(events[0].returncode)
            self.assertEqual(events[1].returncode, 0)
            expected_group_id = events[0].pid if os.name == "posix" else None
            self.assertEqual(events[0].process_group_id, expected_group_id)
            self.assertEqual(events[1].process_group_id, expected_group_id)
        finally:
            result.cleanup_stream_files()

    async def test_process_start_reporting_failure_reaps_spawned_process(
        self,
    ) -> None:
        events: list[InvocationProcessEvent] = []
        created_processes: list[asyncio.subprocess.Process] = []
        workspace_environment_applied_calls = 0
        original_create_subprocess_exec = asyncio.create_subprocess_exec

        async def tracking_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            process = await original_create_subprocess_exec(*args, **kwargs)
            created_processes.append(process)
            return process

        def record_process_event(event: InvocationProcessEvent) -> None:
            events.append(event)
            if event.status == "started":
                raise OSError("cannot record process")

        def record_workspace_environment_applied() -> None:
            nonlocal workspace_environment_applied_calls
            workspace_environment_applied_calls += 1

        context = replace(
            _workspace_invocation_context(
                Path.cwd(),
                record_workspace_environment_applied,
            ),
            process_event_sink=record_process_event,
        )

        with (
            patch(
                "crewplane.runtime.agent.invocation.command.asyncio.create_subprocess_exec",
                new=tracking_create_subprocess_exec,
            ),
            self.assertRaisesRegex(RuntimeError, "process started reporting failed"),
        ):
            await run_command_once(
                cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
                child_environment=ChildProcessEnvironment(set={}, unset=()),
            )

        self.assertEqual(len(created_processes), 1)
        self.assertIsNotNone(created_processes[0].returncode)
        self.assertEqual([event.status for event in events], ["started", "exited"])
        self.assertIsNotNone(events[1].returncode)
        self.assertEqual(workspace_environment_applied_calls, 1)

    async def test_process_exit_reporting_does_not_mask_process_failure(self) -> None:
        def record_process_event(event: InvocationProcessEvent) -> None:
            if event.status == "exited":
                raise OSError("cannot record exit")

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=record_process_event,
        )

        with (
            patch(
                "crewplane.runtime.agent.invocation.command.open_log_handle",
                side_effect=OSError("cannot open log"),
            ),
            self.assertRaisesRegex(
                RuntimeError, "Execution error: cannot open log"
            ) as caught,
        ):
            await run_command_once(
                cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
                stdin_data=None,
                log_file=Path("provider.log"),
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
            )

        self.assertTrue(
            any(
                "Provider process exit reporting failed" in note
                for note in caught.exception.__notes__
            )
        )

    async def test_process_exit_reporting_failure_cleans_stream_capture(self) -> None:
        cleanup_calls = 0
        original_cleanup = ProcessOutputCapture.cleanup

        def record_process_event(event: InvocationProcessEvent) -> None:
            if event.status == "exited":
                raise OSError("cannot record exit")

        def track_cleanup(capture: ProcessOutputCapture) -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            original_cleanup(capture)

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=record_process_event,
        )

        with (
            patch.object(ProcessOutputCapture, "cleanup", new=track_cleanup),
            self.assertRaisesRegex(RuntimeError, "process exited reporting failed"),
        ):
            await run_command_once(
                cmd=[sys.executable, "-c", "print('ok')"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
            )

        self.assertEqual(cleanup_calls, 1)

    async def test_cancellation_reports_process_exit_after_reaping(self) -> None:
        events: list[InvocationProcessEvent] = []
        process_started = asyncio.Event()

        def record_process_event(event: InvocationProcessEvent) -> None:
            events.append(event)
            if event.status == "started":
                process_started.set()

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=record_process_event,
        )
        invocation = asyncio.create_task(
            run_command_once(
                cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=context,
                idle_timeout_seconds=None,
            )
        )
        await asyncio.wait_for(process_started.wait(), timeout=1.0)

        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation

        self.assertEqual([event.status for event in events], ["started", "exited"])
        self.assertIsNotNone(events[1].returncode)

    async def test_run_command_once_does_not_look_up_spawned_process_group(
        self,
    ) -> None:
        if os.name != "posix":
            self.skipTest("process groups are POSIX-only")

        with patch(
            "crewplane.runtime.agent.invocation.command.os.getpgid",
            side_effect=AssertionError("racy process-group lookup"),
        ):
            result = await run_command_once(
                cmd=[sys.executable, "-c", "pass"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
            )

        self.assertEqual(result.returncode, 0)

    async def test_run_command_once_applies_cwd_and_child_environment(self) -> None:
        with patch.dict(os.environ, {"WORKSPACE_TEST_UNSET": "inherited"}):
            result = await run_command_once(
                cmd=[
                    sys.executable,
                    "-c",
                    (
                        "import os, pathlib; "
                        "print(pathlib.Path.cwd()); "
                        "print(os.getenv('WORKSPACE_TEST_SET')); "
                        "print(os.getenv('WORKSPACE_TEST_UNSET'))"
                    ),
                ],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
                child_environment=ChildProcessEnvironment(
                    set={"WORKSPACE_TEST_SET": "applied"},
                    unset=("WORKSPACE_TEST_UNSET",),
                ),
            )

        lines = result.stdout_text.strip().splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(Path(lines[0]), Path.cwd())
        self.assertEqual(lines[1], "applied")
        self.assertEqual(lines[2], "None")

    async def test_workspace_child_environment_preserves_git_transport_controls(
        self,
    ) -> None:
        inherited = {
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_ALLOW_PROTOCOL": "https",
        }
        with patch.dict(os.environ, inherited):
            result = await run_command_once(
                cmd=[
                    sys.executable,
                    "-c",
                    (
                        "import os; "
                        "print(os.getenv('GIT_PROTOCOL_FROM_USER')); "
                        "print(os.getenv('GIT_ALLOW_PROTOCOL'))"
                    ),
                ],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
                child_environment=workspace_child_environment(Path.cwd()),
            )

        self.assertEqual(result.stdout_text.strip().splitlines(), ["0", "https"])

    async def test_run_command_once_records_child_environment_after_spawn(self) -> None:
        record_calls = 0

        def record_child_environment_applied() -> None:
            nonlocal record_calls
            record_calls += 1

        result = await run_command_once(
            cmd=[sys.executable, "-c", "print('ok')"],
            stdin_data=None,
            log_file=None,
            append_log=False,
            log_header=None,
            cwd=Path.cwd(),
            invocation_context=_workspace_invocation_context(
                Path.cwd(),
                record_child_environment_applied,
            ),
            idle_timeout_seconds=None,
            child_environment=ChildProcessEnvironment(set={}, unset=()),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(record_calls, 1)

    async def test_run_command_once_does_not_record_child_environment_when_spawn_fails(
        self,
    ) -> None:
        record_calls = 0

        def record_child_environment_applied() -> None:
            nonlocal record_calls
            record_calls += 1

        with self.assertRaisesRegex(RuntimeError, "CLI executable not found"):
            await run_command_once(
                cmd=[str(Path.cwd() / "definitely-missing-provider-cli")],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=_workspace_invocation_context(
                    Path.cwd(),
                    record_child_environment_applied,
                ),
                idle_timeout_seconds=None,
                child_environment=ChildProcessEnvironment(set={}, unset=()),
            )

        self.assertEqual(record_calls, 0)

    async def test_run_invocation_attempt_passes_idle_timeout_to_runner(self) -> None:
        observed_idle_timeouts: list[float | None] = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=[sys.executable], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )
        runtime = build_invocation_runtime(plan)

        async def runner(
            cmd: list[str],  # noqa: ARG001
            stdin_data: bytes | None,  # noqa: ARG001
            log_file: Path | None,  # noqa: ARG001
            append_log: bool,  # noqa: ARG001
            log_header: bytes | None,  # noqa: ARG001
            cwd: Path,  # noqa: ARG001
            invocation_context: InvocationContext | None,  # noqa: ARG001
            idle_timeout_seconds: float | None,
            child_environment: ChildProcessEnvironment | None = None,  # noqa: ARG001
        ) -> CommandResult:
            observed_idle_timeouts.append(idle_timeout_seconds)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        result = await run_invocation_attempt(
            runtime=runtime,
            command_runner=runner,
            log_file=None,
            attempt=0,
            cwd=Path.cwd(),
            invocation_context=None,
            timeout_seconds=None,
            idle_timeout_seconds=12.5,
            child_environment=None,
        )

        self.assertEqual(result.stdout_text, "ok")
        self.assertEqual(observed_idle_timeouts, [12.5])

    async def test_run_invocation_attempt_emits_timeout_diagnostic(self) -> None:
        diagnostics = []
        plan = build_cli_invocation_plan(
            AgentConfig(cli_cmd=[sys.executable], default_model="test"),
            "test",
            "prompt",
            Path("output.txt"),
        )
        runtime = build_invocation_runtime(plan)

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
            await asyncio.sleep(10)
            return CommandResult(returncode=0, stdout_text="ok", stderr_text="")

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            diagnostics=diagnostics.append,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "wall-clock timeout reached after 0.01s",
        ):
            await run_invocation_attempt(
                runtime=runtime,
                command_runner=runner,
                log_file=None,
                attempt=0,
                cwd=Path.cwd(),
                invocation_context=context,
                timeout_seconds=0.01,
                idle_timeout_seconds=None,
                child_environment=None,
            )

        self.assertEqual(
            [diagnostic.operation for diagnostic in diagnostics], ["invocation_timeout"]
        )
        self.assertEqual(diagnostics[0].attributes["timeout_scope"], "wall_clock")


def _workspace_invocation_context(
    cwd: Path,
    recorder,
) -> InvocationContext:
    return InvocationContext(
        node_id="node.a",
        task_id="generic_executor_0",
        provider="generic",
        role=ProviderRole.EXECUTOR,
        workspace_environment_applied_recorder=recorder,
        workspace=InvocationWorkspaceContext(
            workspace_kind="snapshot",
            materialization="snapshot_checkout",
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
            child_environment_required=True,
            child_environment_applied=False,
        ),
    )
