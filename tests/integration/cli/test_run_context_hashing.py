import io
import os
import tempfile
import unittest
from pathlib import Path

import orchestrator_cli.cli.app as cli
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class CliRunContextHashingTests(unittest.TestCase):
    def test_failed_run_writes_failure_manifest_and_does_not_trigger_skip(self) -> None:
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

            async def flaky_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("simulated failure")

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            cli.execute_workflow = flaky_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        force=False,
                    )

                stage_runs = sorted(
                    (tmp_path / ".orchestrator" / "execution-stages").glob("task-*")
                )
                self.assertEqual(len(stage_runs), 1)
                manifests_dir = stage_runs[0] / "manifests"
                # A failure manifest is now written so the run is recorded.
                self.assertTrue(manifests_dir.exists())
                manifest_files = [
                    f for f in manifests_dir.glob("*.json") if f.name != "latest.json"
                ]
                self.assertEqual(len(manifest_files), 1)
                import json as _json

                manifest_data = _json.loads(
                    manifest_files[0].read_text(encoding="utf-8")
                )
                self.assertEqual(manifest_data.get("status"), "failed")

                # The second run must still execute (failure manifest must not trigger skip).
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

            self.assertEqual(calls["count"], 2)
            self.assertNotIn("Identical context detected", stream.getvalue())

    def test_run_skips_duplicate_context_across_previous_run_folders(self) -> None:
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
                    force=True,
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

            self.assertEqual(calls["count"], 2)
            self.assertIn("Identical context detected", stream.getvalue())
            stage_runs = sorted(
                (tmp_path / ".orchestrator" / "execution-stages").glob("task-*")
            )
            self.assertGreaterEqual(len(stage_runs), 2)

    def test_run_reexecutes_when_env_template_value_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            workflow_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Task",
                        "nodes:",
                        "  - id: review.node",
                        "    mode: parallel",
                        "    providers: [alpha]",
                        "---",
                        "",
                        "## review.node",
                        "",
                        "branch={{env:BRANCH_NAME}}",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            original_branch = os.environ.get("BRANCH_NAME")
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
                os.environ["BRANCH_NAME"] = "feature/one"
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )

                os.environ["BRANCH_NAME"] = "feature/two"
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                    force=False,
                )
            finally:
                if original_branch is None:
                    os.environ.pop("BRANCH_NAME", None)
                else:
                    os.environ["BRANCH_NAME"] = original_branch
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertEqual(calls["count"], 2)
            self.assertNotIn("Identical context detected", stream.getvalue())
