import io
import os
import tempfile
import unittest
from pathlib import Path

import typer

import crewplane.cli.app as cli
from crewplane.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class CliValidateTemplateAndConfigFailureTests(unittest.TestCase):
    def test_validate_fails_fast_for_missing_provider_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            config_path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["definitely-not-installed-cli"]',
                        '    default_model: "model-a"',
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
                        "nodes:",
                        "  - id: review.node",
                        "    mode: parallel",
                        "    providers: [alpha]",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Provider validation failed", output_text)
            self.assertIn("definitely-not-installed-cli", output_text)
            self.assertIn(
                "Provider setup: docs/getting-started/provider-setup.md",
                output_text,
            )

    def test_validate_compiles_without_real_workspace_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_compile = cli.workflow_runner.compile_workflow_preview
            workspace_real_execution_values: list[bool | None] = []

            def recording_compile_workflow_preview(*args: object, **kwargs: object):
                workspace_real_execution_values.append(
                    kwargs.get("workspace_real_execution")
                )
                return original_compile(*args, **kwargs)

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            cli.workflow_runner.compile_workflow_preview = (
                recording_compile_workflow_preview
            )
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.workflow_runner.compile_workflow_preview = original_compile
                cli.Console = original_console_cls

            self.assertEqual(workspace_real_execution_values, [False])

    def test_validate_skips_real_workspace_relative_executable_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            config_path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["./bin/provider"]',
                        "",
                        "settings:",
                        "  workspace:",
                        "    enabled: true",
                        "  integrations:",
                        "    invoker:",
                        '      implementation: "cli"',
                        "      options: {}",
                    ]
                ),
                encoding="utf-8",
            )
            workflow_path.write_text(
                "\n".join(
                    [
                        "---",
                        f'schema_version: "{SCHEMA_VERSION}"',
                        "name: Workspace Task",
                        "worktrees:",
                        "  primary:",
                        "    kind: worktree",
                        "nodes:",
                        "  - id: implement",
                        "    mode: sequential",
                        "    providers: [alpha]",
                        "---",
                        "",
                        "## implement",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Workspace validation failed", output_text)
            self.assertIn("requires a Git repository", output_text)
            self.assertNotIn("relative path executable", output_text)
            self.assertIn("./bin/provider", output_text)

    def test_validate_shows_warning_for_argv_prompt_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            config_path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "model-a"',
                        '    prompt_transport: "argv"',
                        '    prompt_transport_arg: "--prompt"',
                    ]
                ),
                encoding="utf-8",
            )
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight warnings:", output_text)
            self.assertIn("uses argv prompt transport", output_text)

    def test_validate_fails_fast_for_missing_env_template_reference(self) -> None:
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
                        "branch={{env:ORCH_VALIDATE_REQUIRED_ENV}}",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_env = os.environ.get("ORCH_VALIDATE_REQUIRED_ENV")
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
                os.environ.pop("ORCH_VALIDATE_REQUIRED_ENV", None)
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                if original_env is None:
                    os.environ.pop("ORCH_VALIDATE_REQUIRED_ENV", None)
                else:
                    os.environ["ORCH_VALIDATE_REQUIRED_ENV"] = original_env
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertIn(
                "Environment variable not set: ORCH_VALIDATE_REQUIRED_ENV",
                output_text,
            )
            self.assertNotIn("Invalid:", output_text)

    def test_validate_fails_fast_for_missing_var_template_reference(self) -> None:
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
                        "branch={{var:ORCH_VALIDATE_REQUIRED_VAR}}",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertIn(
                "Template variable not set: ORCH_VALIDATE_REQUIRED_VAR", output_text
            )
            self.assertNotIn("Invalid:", output_text)

    def test_validate_fails_fast_for_blocked_file_template_reference(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp_dir,
            tempfile.TemporaryDirectory() as external_dir,
        ):
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            external_file = Path(external_dir) / "external.txt"
            external_file.write_text("secret", encoding="utf-8")
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
                        f"load={{{{file:{external_file}}}}}",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertIn("Template access denied", output_text)
            self.assertNotIn("Invalid:", output_text)

    def test_validate_fails_fast_for_invalid_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            config_path.write_text(
                "\n".join(
                    [
                        f'version: "{SCHEMA_VERSION}"',
                        "",
                        "agents:",
                        "  alpha:",
                        '    cli_cmd: ["echo"]',
                        '    default_model: "model-a"',
                        "settings:",
                        "  token_budget:",
                        "    warn_threshold_chars: 1000",
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
                        "nodes:",
                        "  - id: review.node",
                        "    mode: sequential",
                        "    token_budget:",
                        "      fail_threshold_chars: 900",
                        "    providers:",
                        "      - provider: alpha",
                        "        role: executor",
                        "---",
                        "",
                        "## review.node",
                        "",
                        "Review this.",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Token budget validation failed", output_text)
            self.assertIn("review.node", output_text)
            self.assertNotIn("Valid:", output_text)

    def test_validate_fails_fast_for_missing_file_template_reference(self) -> None:
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
                        "load={{file:missing-template.md}}",
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertIn("File not found: missing-template.md", output_text)
            self.assertNotIn("Invalid:", output_text)

    def test_validate_uses_default_config_and_writes_no_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            original_cwd = Path.cwd()
            os.chdir(tmp_path)
            try:
                cli.validate(tasks_file=workflow_path, config_file=None)
            finally:
                os.chdir(original_cwd)

            self.assertFalse((state_dir / "execution-stages").exists())
            self.assertFalse((state_dir / "execution-results").exists())

    def test_validate_requires_default_config_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            workflow_path = workflows_dir / "workflow.task.md"
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_cwd = Path.cwd()
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            os.chdir(tmp_path)
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=None)
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn(".crewplane/config.yml not found", output_text)
