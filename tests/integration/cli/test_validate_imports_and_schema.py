import io
import os
import tempfile
import unittest
from pathlib import Path

import typer

import orchestrator_cli.cli.app as cli
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class CliValidateImportsAndSchemaTests(unittest.TestCase):
    def test_validate_accepts_workflow_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "example.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Frontmatter: valid YAML", output_text)
            self.assertIn("Schema: WorkflowFrontmatter", output_text)
            self.assertIn("Nodes:", output_text)
            self.assertIn("Dependencies:", output_text)
            self.assertIn("Providers: references resolved", output_text)
            self.assertIn("Preflight: compiled execution plan preview", output_text)
            self.assertIn("Valid:", output_text)

    def test_validate_accepts_workflow_with_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            module_path = tmp_path / "module.task.md"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)

            module_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Auth Module",
                        "nodes:",
                        "  - id: plan",
                        "    mode: sequential",
                        "    providers:",
                        "      - provider: alpha",
                        "        role: executor",
                        "---",
                        "",
                        "## plan",
                        "",
                        "Build {{param:module_name}} for {{var:project_name}}.",
                    ]
                ),
                encoding="utf-8",
            )
            workflow_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Task",
                        "imports:",
                        "  - path: module.task.md",
                        "    as: auth",
                        "    with:",
                        "      module_name: payments-auth",
                        "nodes:",
                        "  - id: summary.final",
                        "    mode: sequential",
                        "    needs: [auth.plan]",
                        "    providers:",
                        "      - provider: alpha",
                        "        role: executor",
                        "---",
                        "",
                        "## summary.final",
                        "",
                        "Summarize {{auth.plan.output}}.",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Imports: 1 imported workflow file(s) resolved", output_text)
            self.assertIn("Valid:", output_text)

    def test_validate_rejects_unused_import_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            module_path = tmp_path / "module.task.md"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)

            module_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Auth Module",
                        "nodes:",
                        "  - id: plan",
                        "    mode: sequential",
                        "    providers:",
                        "      - provider: alpha",
                        "        role: executor",
                        "---",
                        "",
                        "## plan",
                        "",
                        "Build auth module.",
                    ]
                ),
                encoding="utf-8",
            )
            workflow_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Task",
                        "imports:",
                        "  - path: module.task.md",
                        "    as: auth",
                        "    with:",
                        "      module_name: payments-auth",
                        "nodes:",
                        "  - id: summary.final",
                        "    mode: sequential",
                        "    needs: [auth.plan]",
                        "    providers:",
                        "      - provider: alpha",
                        "        role: executor",
                        "---",
                        "",
                        "## summary.final",
                        "",
                        "Summarize {{auth.plan.output}}.",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Invalid:", output_text)
            self.assertIn("unused parameter", output_text)

    def test_validate_fails_fast_for_unknown_provider(self) -> None:
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
                        "    providers: [missing]",
                        "---",
                        "",
                        "## review.node",
                        "",
                        "run",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Provider validation failed", output_text)
            self.assertIn("Unknown provider 'missing'", output_text)
            self.assertNotIn("Invalid:", output_text)

    def test_validate_surfaces_strict_workflow_schema_errors(self) -> None:
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
                        "    mode: sequential",
                        "    providers:",
                        "      - provider: alpha",
                        "        rol: reviewer",
                        "---",
                        "",
                        "## review.node",
                        "",
                        "run",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Invalid:", output_text)
            self.assertIn("Extra inputs are not permitted", output_text)
