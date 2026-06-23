import os
import tempfile
import unittest
from pathlib import Path

import typer

import crewplane.cli.app as cli
from tests.integration.cli.cli_workflow_helpers import (
    write_basic_config,
    write_basic_workflow,
)


class CliWorkflowDiscoveryAndInitTests(unittest.TestCase):
    def test_init_creates_crewplane_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                cli.init()
            finally:
                os.chdir(original_cwd)

            self.assertTrue((tmp_path / ".crewplane" / "config.yml").is_file())
            self.assertTrue((tmp_path / ".crewplane" / "workflows").is_dir())

    def test_run_discovers_single_workflow_markdown_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "code-review-example.task.md"
            nested_workflow_path = (
                workflows_dir / "example-templates" / "design-review-example.task.md"
            )
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)
            nested_workflow_path.parent.mkdir(parents=True)
            write_basic_workflow(nested_workflow_path)

            original_execute_workflow = cli.execute_workflow
            original_cwd = Path.cwd()
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1

            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            os.chdir(tmp_path)
            try:
                cli.run(tasks_file=None, config_file=None, dry_run=False, force=False)
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]

            self.assertEqual(calls["count"], 1)

    def test_run_fails_when_multiple_workflow_files_exist_without_tasks_flag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            write_basic_config(config_path)
            write_basic_workflow(workflows_dir / "one.task.md")
            write_basic_workflow(workflows_dir / "two.task.md")

            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=None, config_file=None, dry_run=False, force=False
                    )
            finally:
                os.chdir(original_cwd)

    def test_run_requires_workflow_file_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            state_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            legacy_tasks_path = state_dir / "tasks.yaml"
            write_basic_config(config_path)
            legacy_tasks_path.write_text("name: legacy", encoding="utf-8")

            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=None, config_file=None, dry_run=False, force=False
                    )
            finally:
                os.chdir(original_cwd)
