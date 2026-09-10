import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.adapters.invokers.cli_invoker import build_cli_invocation_plan
from crewplane.core.config import AgentConfig
from crewplane.runtime.agent.invoker import (
    invoke_agent,
)


class InvokerRetryBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_file_includes_header_and_stream_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_script.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import sys",
                        "print('stdout line')",
                        "print('stderr line', file=sys.stderr)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
            )

            log_content = log_file.read_text(encoding="utf-8")
            assert "started_at:" in log_content
            resolved_python = Path(sys.executable).resolve(strict=True).as_posix()
            assert f"cli_executable: {resolved_python}" in log_content
            assert "model: test-model" in log_content
            assert f"output_file: {output_file}" in log_content
            assert "---" in log_content
            assert "stdout line" in log_content
            assert "[stderr] stderr line" in log_content

    async def test_log_file_uses_provider_default_label_when_model_is_omitted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_script.py"
            script_path.write_text(
                "print('stdout line')\n",
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                model_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                None,
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
            )

            log_content = log_file.read_text(encoding="utf-8")
            assert "model: provider default" in log_content

    async def test_log_file_normalizes_retry_wait_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "log_retry_units_script.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import sys",
                        "print('ok')",
                        "print('Attempt 1 failed: Retrying after 1852.819886ms...', file=sys.stderr)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            await invoke_agent(
                config,
                "test-model",
                "prompt",
                output_file,
                output_file.parent,
                log_file=log_file,
                plan_builder=build_cli_invocation_plan,
            )

            log_content = log_file.read_text(encoding="utf-8")
            assert "[stderr] Attempt 1 failed: Retrying after 1.9s..." in log_content
            assert "1852.819886ms" not in log_content

    async def test_log_setup_failure_reaps_spawned_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "long_running.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import time",
                        "",
                        "time.sleep(10)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            created_processes: list[asyncio.subprocess.Process] = []
            original_create_subprocess_exec = asyncio.create_subprocess_exec

            async def tracking_create_subprocess_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
                process = await original_create_subprocess_exec(*args, **kwargs)
                created_processes.append(process)
                return process

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test-model",
                model_arg=None,
            )
            output_file = tmp_path / "output.txt"
            log_file = tmp_path / "agent.log"

            try:
                with (
                    patch(
                        "crewplane.runtime.agent.invocation.command.asyncio.create_subprocess_exec",
                        new=tracking_create_subprocess_exec,
                    ),
                    patch(
                        "crewplane.runtime.agent.invocation.command.open_log_handle",
                        side_effect=OSError("cannot open log"),
                    ),
                    pytest.raises(RuntimeError, match="Execution error"),
                ):
                    await invoke_agent(
                        config,
                        "test-model",
                        "prompt",
                        output_file,
                        output_file.parent,
                        log_file=log_file,
                        plan_builder=build_cli_invocation_plan,
                    )

                assert len(created_processes) == 1
                process = created_processes[0]
                await asyncio.wait_for(process.wait(), timeout=1.0)
                assert process.returncode is not None
            finally:
                for process in created_processes:
                    if process.returncode is None:
                        process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
