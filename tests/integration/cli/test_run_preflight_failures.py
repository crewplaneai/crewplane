import io
import tempfile
import unittest
from pathlib import Path

import pytest
import typer

import crewplane.cli.app as cli
from crewplane.artifacts.locks import ResumeLockError, SameContextLock
from crewplane.version import SCHEMA_VERSION
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    cli_process_state,
    repo_task_workflow_stage_names,
    write_basic_config,
    write_basic_workflow,
)


class CliRunPreflightFailureTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _repository_root(self, pytestconfig: pytest.Config) -> None:
        self.repository_root = pytestconfig.rootpath

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
        with temporary_project_cwd() as tmp_path:
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
            with cli_process_state(stream):
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                )

            output_text = stream.getvalue()
            self.assertIn("Preflight warnings:", output_text)
            self.assertIn("PROVIDER-CONFIG", output_text)
            self.assertIn("argv prompt transport", output_text)
            self.assertIn(
                "Mock invoker active: no provider CLI commands will be started.",
                output_text,
            )

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
            with cli_process_state(stream), self.assertRaises(typer.Exit):
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                )

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

            def fail_update_run(self, run_id: str, run_key_name: str) -> None:
                del self, run_id, run_key_name
                raise ResumeLockError("Cannot update a lock owned by another process.")

            with cli_process_state(stream) as process_state:
                process_state.setattr(
                    SameContextLock,
                    "update_run",
                    fail_update_run,
                )
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                        no_live=True,
                    )

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
            original_repo_stage_names = repo_task_workflow_stage_names(
                self.repository_root
            )
            with cli_process_state(stream) as process_state:
                process_state.delenv("ORCH_REQUIRED_ENV", raising=False)
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=False,
                    )

            output_text = stream.getvalue()
            self.assertIn("Preflight TEMPLATE-VALUE", output_text)
            self.assertIn(
                "Environment variable not set: ORCH_REQUIRED_ENV", output_text
            )
            self.assertEqual(
                repo_task_workflow_stage_names(self.repository_root),
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
            with cli_process_state(stream):
                cli.run(tasks_file=workflow_path, config_file=config_path, dry_run=True)

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
            with cli_process_state(stream), self.assertRaises(typer.Exit):
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=True,
                )

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
            with cli_process_state(stream) as process_state:
                process_state.delenv("ORCH_REQUIRED_ENV", raising=False)
                with self.assertRaises(typer.Exit):
                    cli.run(
                        tasks_file=workflow_path,
                        config_file=config_path,
                        dry_run=True,
                    )

            output_text = stream.getvalue()
            self.assertIn("Preflight compilation failed", output_text)
            self.assertNotIn("Dry run mode", output_text)

    def test_mock_invoker_run_skips_cli_executable_validation(self) -> None:
        with temporary_project_cwd() as tmp_path:
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
            calls = {"count": 0}

            async def fake_execute_workflow(plan, output, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ARG001 - Required by test double or callback signature.
                calls["count"] += 1

            with cli_process_state(stream) as process_state:
                process_state.setattr(
                    cli,
                    "execute_workflow",
                    fake_execute_workflow,
                )
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=False,
                )

            self.assertEqual(calls["count"], 1)
            output_text = stream.getvalue()
            self.assertIn(
                "Mock invoker active: no provider CLI commands will be started.",
                output_text,
            )
            self.assertNotIn("Provider validation failed", output_text)

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
            with cli_process_state(stream), self.assertRaises(typer.Exit):
                cli.run(
                    tasks_file=workflow_path,
                    config_file=config_path,
                    dry_run=True,
                )

            output_text = stream.getvalue()
            self.assertIn("Audit rounds validation failed", output_text)
            self.assertIn("settings.max_audit_rounds", output_text)
            self.assertNotIn("Provider validation failed", output_text)
            self.assertNotIn("Dry run mode", output_text)
