import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator_cli.adapters.invokers.cli_invoker import build_cli_invocation_plan
from orchestrator_cli.architecture.contracts import (
    ChildProcessEnvironment,
    CommandResult,
    InvocationContext,
    InvocationSourceContext,
    InvocationWorkspaceContext,
    InvocationWorktreeContract,
)
from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.invocation.command import (
    build_invocation_runtime,
    cleanup_structured_output_file,
    prepare_runtime_for_attempt,
    run_command_once,
    run_invocation_attempt,
)
from orchestrator_cli.version import SCHEMA_VERSION


def test_prepare_runtime_for_attempt_clears_stale_structured_output() -> None:
    plan = build_cli_invocation_plan(
        AgentConfig(
            cli_cmd=["codex", "exec"],
            provider_kind="codex",
            default_model="gpt-5.4",
            prompt_transport_arg="-",
        ),
        "gpt-5.4",
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
    async def test_run_command_once_uses_spawned_process_group_id(self) -> None:
        if os.name != "posix":
            self.skipTest("process group lookup is POSIX-only")

        observed_pids: list[int] = []

        def fake_getpgid(pid: int) -> int:
            observed_pids.append(pid)
            return pid

        with patch(
            "orchestrator_cli.runtime.agent.invocation.command.os.getpgid",
            side_effect=fake_getpgid,
        ):
            result = await run_command_once(
                cmd=[sys.executable, "-c", "print('ok')"],
                stdin_data=None,
                log_file=None,
                append_log=False,
                log_header=None,
                cwd=Path.cwd(),
                invocation_context=None,
                idle_timeout_seconds=None,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout_text.strip(), "ok")
        self.assertEqual(len(observed_pids), 1)
        self.assertGreater(observed_pids[0], 0)

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
            role="executor",
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
        role="executor",
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
