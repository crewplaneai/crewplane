import io
import os
import tempfile
import unittest
from pathlib import Path

import typer

import crewplane.cli.app as cli
from crewplane.artifacts.locks import ResumeLockError, SameContextLock
from crewplane.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    repo_task_workflow_stage_names,
    write_basic_config,
    write_basic_workflow,
)


class CliRunPreflightFailureTests(unittest.TestCase):
    def test_run_fails_fast_for_unknown_provider(self) -> None:
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

            with self.assertRaises(typer.Exit):
                cli.run(
                    tasks_file=workflow_path, config_file=config_path, dry_run=False
                )

    def test_run_preflight_shows_warning_for_argv_prompt_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path = tmp_path / "config.yml"
            workflow_path = tmp_path / "workflow.task.md"
            write_basic_config(config_path)
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
                        "settings:",
                        "  integrations:",
                        "    invoker:",
                        '      implementation: "mock"',
                        "      options:",
                        "        delay_seconds: 0",
                        "        observation_delay_seconds: 0",
                    ]
                ),
                encoding="utf-8",
            )
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
            try:
                os.chdir(tmp_path)
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                )
            finally:
                os.chdir(original_cwd)
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight warnings:", output_text)
            self.assertIn("PROVIDER-CONFIG", output_text)
            self.assertIn("argv prompt transport", output_text)

    def test_run_reports_invalid_workflow_without_raw_exception(self) -> None:
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
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                    )
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight PREFLIGHT-VALIDATION", output_text)
            self.assertIn("rendered executor prompt cannot be empty", output_text)
            self.assertNotIn("Traceback", output_text)

    def test_run_reports_resume_lock_failure_without_raw_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_dir = tmp_path / ".crewplane"
            workflows_dir = state_dir / "workflows"
            workflows_dir.mkdir(parents=True)
            config_path = state_dir / "config.yml"
            workflow_path = workflows_dir / "workflow.task.md"
            write_basic_config(config_path)
            write_basic_workflow(workflow_path)

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_update_run = SameContextLock.update_run

            def fail_update_run(self, run_id: str, run_key_name: str) -> None:
                del self, run_id, run_key_name
                raise ResumeLockError("Cannot update a lock owned by another process.")

            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            SameContextLock.update_run = fail_update_run
            try:
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        no_live=True,
                    )
            finally:
                SameContextLock.update_run = original_update_run
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Run lock unavailable", output_text)
            self.assertIn("Cannot update a lock owned by another process", output_text)
            self.assertIn(".crewplane/locks", output_text)
            self.assertNotIn("Traceback", output_text)

    def test_run_fails_fast_for_missing_env_template_reference(self) -> None:
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
                        "branch={{env:ORCH_REQUIRED_ENV}}",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_env = os.environ.get("ORCH_REQUIRED_ENV")
            original_repo_stage_names = repo_task_workflow_stage_names()
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
                os.environ.pop("ORCH_REQUIRED_ENV", None)
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                    )
            finally:
                if original_env is None:
                    os.environ.pop("ORCH_REQUIRED_ENV", None)
                else:
                    os.environ["ORCH_REQUIRED_ENV"] = original_env
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight TEMPLATE-VALUE", output_text)
            self.assertIn(
                "Environment variable not set: ORCH_REQUIRED_ENV", output_text
            )
            self.assertEqual(
                repo_task_workflow_stage_names(),
                original_repo_stage_names,
            )
            self.assertTrue((tmp_path / ".crewplane" / "execution-stages").exists())

    def test_dry_run_skips_cli_executable_validation(self) -> None:
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
                        "  - id: plan.node",
                        "    mode: parallel",
                        "    providers: [alpha]",
                        "---",
                        "",
                        "## plan.node",
                        "",
                        "review",
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
                cli.run(tasks_file=workflow_path, config_file=config_path, dry_run=True)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Dry run mode", output_text)
            self.assertNotIn("Provider validation failed", output_text)

    def test_dry_run_fails_for_unknown_provider_before_printing_plan(self) -> None:
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
                        "  - id: plan.node",
                        "    mode: parallel",
                        "    providers: [missing]",
                        "---",
                        "",
                        "## plan.node",
                        "",
                        "review",
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
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=True,
                    )
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Provider validation failed", output_text)
            self.assertNotIn("Dry run mode", output_text)

    def test_dry_run_fails_for_missing_env_template_before_printing_plan(self) -> None:
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
                        "  - id: plan.node",
                        "    mode: parallel",
                        "    providers: [alpha]",
                        "---",
                        "",
                        "## plan.node",
                        "",
                        "branch={{env:ORCH_REQUIRED_ENV}}",
                    ]
                ),
                encoding="utf-8",
            )

            stream = io.StringIO()
            original_console_cls = cli.Console
            original_env = os.environ.get("ORCH_REQUIRED_ENV")
            cli.Console = ConsoleFactory(
                file=stream,
                force_terminal=False,
                color_system=None,
                width=120,
            )
            try:
                os.environ.pop("ORCH_REQUIRED_ENV", None)
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=True,
                    )
            finally:
                if original_env is None:
                    os.environ.pop("ORCH_REQUIRED_ENV", None)
                else:
                    os.environ["ORCH_REQUIRED_ENV"] = original_env
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertNotIn("Dry run mode", output_text)

    def test_mock_invoker_run_skips_cli_executable_validation(self) -> None:
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
                        "settings:",
                        "  integrations:",
                        "    invoker:",
                        '      implementation: "mock"',
                        "      options:",
                        "        delay_seconds: 0",
                        "        observation_delay_seconds: 0",
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
                        "  - id: plan.node",
                        "    mode: parallel",
                        "    providers: [alpha]",
                        "---",
                        "",
                        "## plan.node",
                        "",
                        "review",
                    ]
                ),
                encoding="utf-8",
            )

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
                )
            finally:
                os.chdir(original_cwd)
                cli.execute_workflow = original_execute_workflow  # type: ignore[assignment]
                cli.Console = original_console_cls

            self.assertEqual(calls["count"], 1)
            self.assertNotIn("Provider validation failed", stream.getvalue())

    def test_dry_run_validates_audit_rounds_max_without_cli_validation(self) -> None:
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
                        "  exec:",
                        '    cli_cmd: ["definitely-not-installed-exec"]',
                        '    default_model: "model-a"',
                        "  review:",
                        '    cli_cmd: ["definitely-not-installed-review"]',
                        '    default_model: "model-b"',
                        "settings:",
                        "  max_audit_rounds: 1",
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
                        "  - id: review.iterate",
                        "    mode: sequential",
                        "    audit_rounds: 2",
                        "    providers:",
                        "      - provider: exec",
                        "        role: executor",
                        "      - provider: review",
                        "        role: reviewer",
                        "---",
                        "",
                        "## review.iterate",
                        "",
                        "review",
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
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=True,
                    )
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Audit rounds validation failed", output_text)
            self.assertIn("settings.max_audit_rounds", output_text)
            self.assertNotIn("Provider validation failed", output_text)
            self.assertNotIn("Dry run mode", output_text)
