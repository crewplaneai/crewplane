import io
import unittest
from pathlib import Path
from tempfile import mkdtemp

import pytest
import typer

import crewplane.cli.app as cli
from crewplane.artifacts.locks import ResumeLockError, SameContextLock
from crewplane.version import SCHEMA_VERSION
from tests.helpers.config_secrets import CONFIG_FAILURES
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    cli_process_state,
    repo_task_workflow_stage_names,
    write_basic_config,
    write_basic_workflow,
)

pytestmark = pytest.mark.usefixtures("posix_cli_plans")


class CliRunPreflightFailureTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    @pytest.fixture(autouse=True)
    def _repository_root(self, pytestconfig: pytest.Config) -> None:
        self.repository_root = pytestconfig.rootpath

    def test_run_fails_fast_for_unknown_provider(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
            cli.run(tasks_file=workflow_path, config_file=config_path, dry_run=False)

    def test_run_preflight_shows_warning_for_argv_prompt_transport(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
            del run_id, run_key_name
            raise ResumeLockError(
                "Cannot update a lock owned by another process: "
                f"{self.lock_dir.as_posix()}"
            )

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
        self.assertIn(".crewplane/locks", "".join(output_text.split()))
        self.assertNotIn("Traceback", output_text)

    def test_run_fails_fast_for_missing_env_template_reference(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        original_repo_stage_names = repo_task_workflow_stage_names(self.repository_root)
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
        self.assertIn("Environment variable not set: ORCH_REQUIRED_ENV", output_text)
        self.assertEqual(
            repo_task_workflow_stage_names(self.repository_root),
            original_repo_stage_names,
        )
        self.assertTrue((tmp_path / ".crewplane" / "execution-stages").exists())

    def test_dry_run_skips_cli_executable_validation(self) -> None:
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
        tmp_dir = mkdtemp(dir=self.tmp_path)
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


@pytest.mark.parametrize("command", ["validate", "dry-run", "run", "cleanup"])
@pytest.mark.parametrize("case", CONFIG_FAILURES)
def test_config_failure_is_safe_across_cli_entry_points(
    tmp_path, monkeypatch, command, case
):
    from typer.testing import CliRunner

    from tests.helpers.config_secrets import assert_no_secret

    monkeypatch.chdir(tmp_path)
    state = tmp_path / ".crewplane"
    state.mkdir()
    config = state / "config.yml"
    content, location = CONFIG_FAILURES[case]
    config.write_bytes(content.encode())
    workflow = tmp_path / "workflow.task.md"
    write_basic_workflow(workflow)
    args = (
        ["cleanup", "workspaces", "--dry-run"]
        if command == "cleanup"
        else [
            *(
                ["validate", str(workflow)]
                if command == "validate"
                else ["run", "--tasks", str(workflow)]
            )
        ]
    )
    if command == "dry-run":
        args.append("--dry-run")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1, result.output
    assert_no_secret(result.stdout + result.stderr)
    assert location in " ".join(result.output.split())
    files = [
        path
        for root in ("execution-stages", "execution-results")
        for path in (state / root).rglob("*")
        if path.is_file()
    ]
    for path in files:
        assert_no_secret(path.read_bytes())
        assert path.name != "run.json"
        assert path.parent.name == "preflight"
    assert bool(files) is (command == "run")


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
@pytest.mark.parametrize("role", ["executors", "[bold]executors[/bold]"])
def test_workflow_validation_error_is_consistent_across_cli_entry_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, role: str
) -> None:
    import json
    from unittest.mock import AsyncMock

    from typer.testing import CliRunner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLUMNS", "240")
    execution = AsyncMock()
    monkeypatch.setattr(cli, "execute_workflow", execution)
    state = tmp_path / ".crewplane"
    state.mkdir()
    write_basic_config(state / "config.yml")
    workflow = tmp_path / "[red]workflow.task.md"
    workflow.write_text(
        "---\nname: Design\nnodes:\n  - id: design\n    mode: sequential\n"
        "    providers:\n      - provider: alpha\n"
        f"        role: '{role}'\n"
        "        model: private-model-value\n---\n\n## design\nPrivate prompt.\n",
        encoding="utf-8",
        newline="\n",
    )
    args = (
        ["validate", str(workflow)]
        if command == "validate"
        else ["run", "--tasks", str(workflow)]
    )
    if command == "dry-run":
        args.append("--dry-run")

    result = CliRunner().invoke(cli.app, args)

    expected = (
        f"{workflow}:8:15\n"
        "  Node 'design', provider #1 ('alpha'), role:\n"
        f"    Invalid value {role!r}.\n"
        "    Allowed values: executor, reviewer."
    )
    assert result.exit_code == 1, result.output
    assert expected in result.output
    for hidden in (
        "private-model-value",
        "Private prompt.",
        "ProviderSpec",
        "WorkflowFrontmatter",
        "pydantic.dev",
        "Suggested correction:",
    ):
        assert hidden not in result.output
    execution.assert_not_awaited()
    files = [
        path
        for directory in ("execution-results", "execution-stages")
        for path in (state / directory).rglob("*")
        if path.is_file()
    ]
    assert bool(files) is (command == "run")
    assert all(path.parent.name == "preflight" for path in files)
    if command == "run":
        diagnostics_path = next(
            path for path in files if path.name == "diagnostics.json"
        )
        diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        assert diagnostics[0]["message"] == expected
