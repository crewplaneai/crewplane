import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.architecture.contracts import (
    ChildProcessEnvironment,
    InvocationContext,
    InvocationProcessEvent,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.agent.invocation.command import (
    run_command_once,
)
from crewplane.runtime.agent.process.drain import (
    ProcessDrainError,
    ProcessDrainEvidence,
)
from crewplane.runtime.agent.process.stream_capture import ProcessOutputCapture
from tests.integration.runtime.agent.invocation_command_support import (
    command_workspace_context,
)


class InvocationCommandTests(unittest.IsolatedAsyncioTestCase):
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
            command_workspace_context(
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
            pytest.raises(RuntimeError, match="process started reporting failed"),
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

        assert len(created_processes) == 1
        assert created_processes[0].returncode is not None
        assert [event.status for event in events] == ["started", "exited"]
        assert events[1].returncode is not None
        assert workspace_environment_applied_calls == 1

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
            pytest.raises(
                RuntimeError, match="Execution error: cannot open log"
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

        assert any(
            "Provider process exit reporting failed" in note
            for note in caught.value.__notes__
        )

    async def test_drain_failure_precedes_exit_reporting_failure(self) -> None:
        def record_process_event(event: InvocationProcessEvent) -> None:
            if event.status == "exited":
                raise OSError("cannot record exit")

        async def fail_collection(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("provider output collection failed")

        async def fail_drain(
            process: asyncio.subprocess.Process,
            process_group_id: int | None,
            diagnostic_sink: object,
        ) -> None:
            del diagnostic_sink
            process.kill()
            await process.wait()
            raise ProcessDrainError(
                ProcessDrainEvidence(
                    pid=process.pid,
                    process_group_id=process_group_id,
                    leader_stopped=True,
                    process_group_stopped=False,
                ),
                "provider process group remained live",
            )

        context = InvocationContext(
            node_id="node.a",
            task_id="generic_executor_0",
            provider="generic",
            role=ProviderRole.EXECUTOR,
            process_event_sink=record_process_event,
        )

        with (
            patch(
                "crewplane.runtime.agent.invocation.command."
                "write_stdin_and_collect_output",
                new=fail_collection,
            ),
            patch(
                "crewplane.runtime.agent.invocation.command.reap_failed_process",
                new=fail_drain,
            ),
            pytest.raises(ProcessDrainError) as caught,
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
            )

        assert str(caught.value) == "provider process group remained live"
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert str(caught.value.__cause__) == "provider output collection failed"
        assert caught.value.__notes__ == [
            "Provider process exit reporting failed: Provider process exited "
            "reporting failed: cannot record exit"
        ]

    async def test_file_not_found_after_spawn_reaps_process(self) -> None:
        created_processes: list[asyncio.subprocess.Process] = []
        original_create_subprocess_exec = asyncio.create_subprocess_exec

        async def tracking_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            process = await original_create_subprocess_exec(*args, **kwargs)
            created_processes.append(process)
            return process

        with (
            patch(
                "crewplane.runtime.agent.invocation.command.asyncio.create_subprocess_exec",
                new=tracking_create_subprocess_exec,
            ),
            patch(
                "crewplane.runtime.agent.invocation.command.open_log_handle",
                side_effect=FileNotFoundError("log directory disappeared"),
            ),
            pytest.raises(
                RuntimeError, match="Execution error: log directory disappeared"
            ),
        ):
            await run_command_once(
                cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
                stdin_data=None,
                log_file=Path("provider.log"),
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
            )

        assert len(created_processes) == 1
        assert created_processes[0].returncode is not None

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
            pytest.raises(RuntimeError, match="process exited reporting failed"),
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

        assert cleanup_calls == 1
