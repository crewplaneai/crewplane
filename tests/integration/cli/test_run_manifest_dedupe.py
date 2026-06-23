import io
import os
import re
import tempfile
import unittest
from pathlib import Path

import crewplane.cli.app as cli
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
    write_workflow_with_name,
)


class CliRunManifestDedupeTests(unittest.TestCase):
    def test_run_skips_duplicate_context_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertEqual(calls["count"], 1)
            self.assertIn("Identical context detected", stream.getvalue())
            stage_runs = sorted(
                (tmp_path / ".crewplane" / "execution-stages").glob("task-*")
            )
            result_runs = sorted(
                (tmp_path / ".crewplane" / "execution-results").glob("task-*")
            )
            self.assertEqual(len(stage_runs), 1)
            self.assertEqual(result_runs, [])

    def test_run_force_executes_duplicate_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1

            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=True,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]

            self.assertEqual(calls["count"], 2)

    def test_run_prints_artifact_paths_with_sanitized_workflow_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_workflow_with_name(workflow_path, "Review apps")

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                return None

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Workflow: Review apps", output_text)
            self.assertIn("Artifact key: review-apps", output_text)
            stages_root = str(tmp_path / ".crewplane" / "execution-stages")
            results_root = str(tmp_path / ".crewplane" / "execution-results")
            self.assertRegex(
                output_text,
                re.escape(stages_root)
                + r"/review-apps--[0-9a-f]{12}-\d{8}-\d{6}(?:-\d{6})?",
            )
            self.assertRegex(
                output_text,
                re.escape(results_root)
                + r"/review-apps--[0-9a-f]{12}-\d{8}-\d{6}(?:-\d{6})?",
            )
