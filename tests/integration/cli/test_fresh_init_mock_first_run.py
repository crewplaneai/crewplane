import io
import unittest
from pathlib import Path

import pytest

import crewplane.cli.app as cli
from crewplane.core.config import load_config
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import ConsoleFactory


class FreshInitMockFirstRunTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_fresh_init_validate_and_run_no_live_succeeds_with_mock(self) -> None:
        with temporary_project_cwd(self.tmp_path) as root:
            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
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
