import io
import os
import tempfile
import unittest
from pathlib import Path

import crewplane.cli.app as cli
from crewplane.core.config import load_config
from tests.integration.cli.cli_workflow_helpers import ConsoleFactory


class FreshInitMockFirstRunTests(unittest.TestCase):
    def test_fresh_init_validate_and_run_no_live_succeeds_with_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            stream = io.StringIO()
            original_cwd = Path.cwd()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
                os.chdir(root)
                cli.init()
                cli.validate(tasks_file=None, config_file=None)
                cli.run(
                    tasks_file=None,
                    config_file=None,
                    dry_run=False,
                    force=False,
                    no_live=True,
                )
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            config = load_config(root / ".crewplane" / "config.yml")
            assert config.settings is not None
            self.assertEqual(list(config.agents), ["mock"])
            self.assertEqual(
                config.agents["mock"].cli_cmd,
                ["__crewplane_mock_invoker_never_executes__"],
            )
            self.assertEqual(
                config.settings.integrations.invoker.implementation, "mock"
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in (root / ".crewplane" / "workflows").glob("*.task.md")
                ),
                ["single-agent-review.task.md"],
            )

            output_text = stream.getvalue()
            self.assertIn(
                "First run uses deterministic mock execution; no provider CLIs are required.",
                output_text,
            )
            self.assertIn("Real provider runs later:", output_text)
            self.assertIn("which claude codex gemini copilot", output_text)
            self.assertIn(
                'settings.integrations.invoker.implementation: "cli"',
                output_text,
            )
            self.assertIn(".crewplane/workflows/example-templates/", output_text)
            self.assertIn(
                "crewplane run --tasks "
                ".crewplane/workflows/example-templates/code-review-example.task.md",
                output_text,
            )
            self.assertIn(
                "https://github.com/crewplaneai/crewplane/blob/master/docs/index.md",
                output_text,
            )
            self.assertLess(
                output_text.index("Real provider runs later:"),
                output_text.index("Next:"),
            )
            self.assertIn("crewplane validate", output_text)
            self.assertIn("crewplane run --no-live", output_text)
            self.assertIn(
                "Mock invoker active: no provider CLI commands will be started.",
                output_text,
            )

            stage_runs = sorted(
                path
                for path in (root / ".crewplane" / "execution-stages").iterdir()
                if path.is_dir()
            )
            result_runs = sorted(
                path
                for path in (root / ".crewplane" / "execution-results").iterdir()
                if path.is_dir()
            )
            self.assertEqual(len(stage_runs), 1)
            self.assertEqual(len(result_runs), 1)
            self.assertTrue((stage_runs[0] / "logs" / "summary.md").is_file())
            self.assertTrue((stage_runs[0] / "logs" / "events.ndjson").is_file())

            result_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(result_runs[0].glob("*-result.md"))
            )
            self.assertIn("# Mock Invocation Output", result_text)
            self.assertIn("Behavior path: mock invoker lorem mode", result_text)
