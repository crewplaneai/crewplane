import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import orchestrator_cli.cli.app as cli
from orchestrator_cli.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    project_pythonpath,
    write_basic_config_without_default_model,
    write_basic_workflow_with_provider_model,
    write_review_workflow,
)
from tests.integration.cli.dry_run_helpers import run_dry_run, write_standard_project


class CliDryRunTests(unittest.TestCase):
    def test_cli_command_runtime_defaults_are_plain_values(self) -> None:
        run_signature = inspect.signature(cli.run)
        validate_signature = inspect.signature(cli.validate)

        self.assertIsNone(run_signature.parameters["tasks_file"].default)
        self.assertIsNone(run_signature.parameters["config_file"].default)
        self.assertIs(run_signature.parameters["dry_run"].default, False)
        self.assertIs(run_signature.parameters["force"].default, False)
        self.assertIs(run_signature.parameters["no_live"].default, False)
        self.assertIsNone(validate_signature.parameters["tasks_file"].default)
        self.assertIsNone(validate_signature.parameters["config_file"].default)

    def test_cli_module_imports_in_fresh_interpreter(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from orchestrator_cli.cli.app import app; print(type(app).__name__)",
            ],
            capture_output=True,
            check=False,
            cwd=Path(__file__).resolve().parent.parent,
            env={**os.environ, "PYTHONPATH": project_pythonpath()},
            text=True,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=result.stderr or result.stdout,
        )
        self.assertIn("Typer", result.stdout)

    def test_dry_run_shows_provider_roles_and_waves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                workflow_writer=write_review_workflow,
            )

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Wave 1", output_text)
            self.assertIn("[executor]", output_text)
            self.assertIn("[reviewer]", output_text)
            self.assertNotIn("Run Summary", output_text)

    def test_dry_run_shows_provider_default_when_default_model_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                config_writer=write_basic_config_without_default_model,
            )

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("provider default", output_text)

    def test_dry_run_prefers_workflow_provider_model_over_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                workflow_writer=_write_workflow_provider_model,
            )

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("(workflow-model)", output_text)
            self.assertNotIn("(model-a)", output_text)

    def test_dry_run_shows_input_node_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = write_standard_project(
                tmp_path,
                workflow_writer=_write_input_node_workflow,
            )
            input_file = tmp_path / ".orchestrator" / "inputs" / "review-findings.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("review findings", encoding="utf-8")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Node: review-input (input)", output_text)
            self.assertIn(
                "source: {{file:.orchestrator/inputs/review-findings.md}}",
                output_text,
            )


def _write_workflow_provider_model(path: Path) -> None:
    write_basic_workflow_with_provider_model(path, model="workflow-model")


def _write_input_node_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Task",
                "inputs:",
                "  review_input: review-input",
                "nodes:",
                "  - id: review-input",
                "    mode: input",
                '    source: "{{file:.orchestrator/inputs/review-findings.md}}"',
                "  - id: fix.apply",
                "    mode: sequential",
                "    needs: [review-input]",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "---",
                "",
                "## fix.apply",
                "",
                "Use {{review-input.output}}.",
            ]
        ),
        encoding="utf-8",
    )
