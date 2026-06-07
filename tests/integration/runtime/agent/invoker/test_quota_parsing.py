import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from orchestrator_cli.core.config import AgentConfig
from orchestrator_cli.runtime.agent.failures import (
    InvocationFailureError,
)
from orchestrator_cli.runtime.agent.invoker import (
    invoke_agent,
)
from orchestrator_cli.runtime.agent.quota.waits import extract_wait_candidates_from_line
from orchestrator_cli.runtime.agent.retry_units import (
    normalize_retry_wait_units_in_text,
)


class QuotaParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_specific_codex_duration_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_file = tmp_path / "state.txt"
            script_path = tmp_path / "codex_quota_once.py"
            script_path.write_text(
                "\n".join(
                    [
                        "import os",
                        "import sys",
                        "from pathlib import Path",
                        "",
                        "state_path = Path(os.environ['STATE_FILE'])",
                        "count = int(state_path.read_text()) if state_path.exists() else 0",
                        "count += 1",
                        "state_path.write_text(str(count))",
                        "if count < 2:",
                        "    print('usage limit exceeded. Please try again in 45.622s')",
                        "    sys.exit(0)",
                        "print('ok')",
                    ]
                ),
                encoding="utf-8",
            )

            original_state = os.environ.get("STATE_FILE")
            os.environ["STATE_FILE"] = str(state_file)
            try:
                config = AgentConfig(
                    cli_cmd=[sys.executable, str(script_path)],
                    default_model="test",
                    model_arg=None,
                    prompt_arg=None,
                    quota_parser="codex",
                    quota_reached_retry_delay_seconds=0,
                )
                output_file = tmp_path / "output.txt"
                sleep_mock = AsyncMock()
                with patch(
                    "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                    sleep_mock,
                ):
                    await invoke_agent(config, "test-model", "prompt", output_file)
            finally:
                if original_state is None:
                    os.environ.pop("STATE_FILE", None)
                else:
                    os.environ["STATE_FILE"] = original_state

            self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
            self.assertEqual(sleep_mock.await_count, 1)
            sleep_seconds = float(sleep_mock.await_args.args[0])
            self.assertAlmostEqual(sleep_seconds, 50.622, delta=0.2)

    async def test_provider_specific_codex_long_worded_duration_fails_fast(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            script_path = tmp_path / "codex_long_quota.py"
            script_path.write_text(
                "print('usage limit exceeded. Your quota will reset after 4 days 2 hours 46 minutes.')\n",
                encoding="utf-8",
            )

            config = AgentConfig(
                cli_cmd=[sys.executable, str(script_path)],
                default_model="test",
                model_arg=None,
                prompt_arg=None,
                quota_parser="codex",
            )
            output_file = tmp_path / "output.txt"
            with self.assertRaisesRegex(RuntimeError, "exceeds 5 hours") as caught:
                await invoke_agent(config, "test-model", "prompt", output_file)
            self.assertIsInstance(caught.exception, InvocationFailureError)
            failure = caught.exception
            assert isinstance(failure, InvocationFailureError)
            self.assertEqual(failure.kind, "quota_or_rate_limit")
            self.assertEqual(failure.phase, "provider_transport")
            self.assertFalse(output_file.exists())

    async def test_provider_specific_copilot_and_kilo_duration_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            for parser_name in ("copilot", "kilo"):
                state_file = tmp_path / f"{parser_name}_state.txt"
                script_path = tmp_path / f"{parser_name}_quota_once.py"
                script_path.write_text(
                    "\n".join(
                        [
                            "import os",
                            "import sys",
                            "from pathlib import Path",
                            "",
                            "state_path = Path(os.environ['STATE_FILE'])",
                            "count = int(state_path.read_text()) if state_path.exists() else 0",
                            "count += 1",
                            "state_path.write_text(str(count))",
                            "if count < 2:",
                            "    print('rate limit reached, retry after 3s')",
                            "    sys.exit(0)",
                            "print('ok')",
                        ]
                    ),
                    encoding="utf-8",
                )

                original_state = os.environ.get("STATE_FILE")
                os.environ["STATE_FILE"] = str(state_file)
                try:
                    config = AgentConfig(
                        cli_cmd=[sys.executable, str(script_path)],
                        default_model="test",
                        model_arg=None,
                        prompt_arg=None,
                        quota_parser=parser_name,
                        quota_reached_retry_delay_seconds=0,
                    )
                    output_file = tmp_path / f"{parser_name}_output.txt"
                    sleep_mock = AsyncMock()
                    with patch(
                        "orchestrator_cli.runtime.agent.invocation.loop.asyncio.sleep",
                        sleep_mock,
                    ):
                        await invoke_agent(config, "test-model", "prompt", output_file)
                finally:
                    if original_state is None:
                        os.environ.pop("STATE_FILE", None)
                    else:
                        os.environ["STATE_FILE"] = original_state

                self.assertEqual(output_file.read_text(encoding="utf-8").strip(), "ok")
                self.assertEqual(sleep_mock.await_count, 1)
                sleep_seconds = float(sleep_mock.await_args.args[0])
                self.assertAlmostEqual(sleep_seconds, 8.0, delta=0.2)

    def test_parses_claude_reset_at_local_time_with_timezone(self) -> None:
        now_utc = datetime(2026, 4, 10, 17, 0, 0, tzinfo=UTC)
        line = "Your limit will reset at 2pm (America/New_York)."
        waits = extract_wait_candidates_from_line(line, now_utc)
        self.assertTrue(waits)
        self.assertAlmostEqual(max(waits), 3600.0, delta=1.0)

    def test_parses_epoch_pipe_reset_hint(self) -> None:
        now_utc = datetime(2026, 4, 10, 0, 0, 0, tzinfo=UTC)
        line = "quota reached |1893456000"
        waits = extract_wait_candidates_from_line(line, now_utc)
        self.assertTrue(waits)
        self.assertGreater(max(waits), 0)

    def test_normalizes_retrying_after_milliseconds_to_human_readable_seconds(
        self,
    ) -> None:
        line = "Attempt 1 failed. Retrying after 1852.819886ms..."
        normalized = normalize_retry_wait_units_in_text(line)
        self.assertIn("Retrying after 1.9s", normalized)
        self.assertNotIn("1852.819886ms", normalized)
