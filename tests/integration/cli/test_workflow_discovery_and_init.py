import unittest

import typer

import crewplane.cli.app as cli
import crewplane.cli.templates as templates
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    write_basic_config,
    write_basic_workflow,
)


class CliWorkflowDiscoveryAndInitTests(unittest.TestCase):
    def test_init_creates_crewplane_state(self) -> None:
        with temporary_project_cwd() as tmp_path:
            cli.init()

            self.assertTrue((tmp_path / ".crewplane" / "config.yml").is_file())
            workflows_dir = tmp_path / ".crewplane" / "workflows"
            self.assertTrue((workflows_dir / "single-agent-review.task.md").is_file())
            library_dir = workflows_dir / "example-templates"
            template_dir = templates.WORKFLOW_LIBRARY_TEMPLATE_DIR
            expected_assets = {
                path.relative_to(template_dir)
                for path in template_dir.rglob("*")
                if path.is_file()
            }
            self.assertTrue(expected_assets)
            self.assertEqual(
                {
                    path.relative_to(library_dir)
                    for path in library_dir.rglob("*")
                    if path.is_file()
                },
                expected_assets,
            )
            for relative_path in expected_assets:
                with self.subTest(asset=relative_path):
                    self.assertNotIn(
                        "__SCHEMA_VERSION__",
                        (library_dir / relative_path).read_text(encoding="utf-8"),
                    )

    def test_init_preserves_existing_example_assets(self) -> None:
        with temporary_project_cwd() as tmp_path:
            cli.init()
            library_dir = tmp_path / ".crewplane" / "workflows" / "example-templates"
            assets = [path for path in library_dir.rglob("*") if path.is_file()]
            customized_content = "# My customized example\n"
            for asset in assets:
                asset.write_text(customized_content, encoding="utf-8")

            cli.init()

            for asset in assets:
                with self.subTest(asset=asset.relative_to(library_dir)):
                    self.assertEqual(
                        asset.read_text(encoding="utf-8"), customized_content
                    )

    def test_run_discovers_single_workflow_markdown_by_default(self) -> None:
        with temporary_project_cwd() as tmp_path:
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "single-agent-review.task.md"
            nested_workflow_path = (
                workflows_dir / "example-templates" / "design-review-example.task.md"
            )
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)
            nested_workflow_path.parent.mkdir(parents=True)
            write_basic_workflow(nested_workflow_path)

            original_execute_workflow = cli.execute_workflow
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1

            cli.execute_workflow = fake_execute_workflow  # type: ignore[assignment]
            try:
                cli.run(tasks_file=None, config_file=None, dry_run=False, force=False)
            finally:
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]

            self.assertEqual(calls["count"], 1)

    def test_run_fails_when_multiple_workflow_files_exist_without_tasks_flag(
        self,
    ) -> None:
        with temporary_project_cwd() as tmp_path:
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            write_basic_config(config_path)
            write_basic_workflow(workflows_dir / "one.task.md")
            write_basic_workflow(workflows_dir / "two.task.md")

            with self.assertRaises(typer.Exit):
                cli.run(tasks_file=None, config_file=None, dry_run=False, force=False)

    def test_run_requires_workflow_file_by_default(self) -> None:
        with temporary_project_cwd() as tmp_path:
            state_dir = tmp_path / ".crewplane"
            state_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            legacy_tasks_path = state_dir / "tasks.yaml"
            write_basic_config(config_path)
            legacy_tasks_path.write_text("name: legacy", encoding="utf-8")

            with self.assertRaises(typer.Exit):
                cli.run(tasks_file=None, config_file=None, dry_run=False, force=False)
