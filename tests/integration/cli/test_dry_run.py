import inspect
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import crewplane.cli.app as cli
from crewplane.cli.run.workspace.git_source import (
    GIT_MIN_VERSION,
    parse_git_version,
)
from crewplane.version import SCHEMA_VERSION
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    project_pythonpath,
    write_basic_config_without_default_model,
    write_basic_workflow_with_provider_model,
    write_review_workflow,
)
from tests.integration.cli.dry_run_helpers import (
    artifact_tree,
    run_dry_run,
    write_standard_project,
)


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
                "from crewplane.cli.app import app; print(type(app).__name__)",
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
            input_file = tmp_path / ".crewplane" / "inputs" / "review-findings.md"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_text("review findings", encoding="utf-8")

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Node: review-input (input)", output_text)
            self.assertIn(
                "source: {{file:.crewplane/inputs/review-findings.md}}",
                output_text,
            )

    def test_workspace_enabled_dry_run_succeeds_without_artifacts(
        self,
    ) -> None:
        self._skip_without_workspace_git()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = _write_workspace_enabled_project(tmp_path)
            _commit_workspace_project(tmp_path)

            output_text = run_dry_run(tmp_path, config_path, workflow_path)

            self.assertIn("Dry run mode", output_text)
            self.assertIn("Workspace: enabled", output_text)
            self.assertIn("contract: blob_exact", output_text)
            self.assertIn("source: commit=", output_text)
            self.assertIn("invoker: mock launch=mock_no_child_process", output_text)
            self.assertIn("rendered workspace files:", output_text)
            self.assertIn("project_initial=", output_text)
            self.assertIn("cleanup: cleanup_on_success=True", output_text)
            self.assertIn(
                "workspace: snapshot name=scratch source=project",
                output_text,
            )
            self.assertIn("result=discarded_snapshot_drift", output_text)
            self.assertIn("review.node", output_text)
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), ())

    def test_workspace_enabled_validate_succeeds_without_artifacts(
        self,
    ) -> None:
        self._skip_without_workspace_git()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config_path, workflow_path = _write_workspace_enabled_project(tmp_path)
            _commit_workspace_project(tmp_path)
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

            self.assertIn("✓ Valid", stream.getvalue())
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), ())

    def _skip_without_workspace_git(self) -> None:
        try:
            version_text = subprocess.run(
                ["git", "--version"],
                check=True,
                capture_output=True,
            ).stdout.decode("utf-8")
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("git is unavailable")
        version = parse_git_version(version_text)
        if version is None or version < GIT_MIN_VERSION:
            self.skipTest("Git 2.34.1+ is required for workspace source policy")


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
                '    source: "{{file:.crewplane/inputs/review-findings.md}}"',
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


def _write_workspace_enabled_project(root: Path) -> tuple[Path, Path]:
    state_dir = root / ".crewplane"
    workflow_dir = state_dir / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "requirements.md").write_text("requirements\n", encoding="utf-8")
    config_path = state_dir / "config.yml"
    workflow_path = workflow_dir / "workflow.task.md"
    _write_workspace_enabled_config(config_path)
    _write_workspace_file_workflow(workflow_path)
    return config_path, workflow_path


def _write_workspace_enabled_config(path: Path) -> None:
    root = path.parent.parent
    cache_root = root.parent / f"{root.name}-workspace-cache"
    path.write_text(
        "\n".join(
            [
                f'version: "{SCHEMA_VERSION}"',
                "",
                "agents:",
                "  alpha:",
                '    cli_cmd: ["echo"]',
                '    default_model: "model-a"',
                "settings:",
                "  workspace:",
                "    enabled: true",
                f'    cache_root: "{cache_root.as_posix()}"',
                "  integrations:",
                "    invoker:",
                '      implementation: "mock"',
                "      options:",
                "        output_mode: echo",
                "        observation_delay_seconds: 0",
                "    ui:",
                '      implementation: "none"',
                "      options: {}",
            ]
        ),
        encoding="utf-8",
    )


def _write_workspace_file_workflow(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "---",
                f'schema_version: "{SCHEMA_VERSION}"',
                "name: Workspace Task",
                "worktrees:",
                "  scratch:",
                "    kind: snapshot",
                "nodes:",
                "  - id: review.node",
                "    mode: sequential",
                "    worktree: scratch",
                "    providers:",
                "      - provider: alpha",
                "        role: executor",
                "---",
                "",
                "## review.node",
                "",
                "Read {{file:docs/requirements.md}}.",
            ]
        ),
        encoding="utf-8",
    )


def _commit_workspace_project(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Crewplane Test")
    _git(root, "config", "user.email", "crewplane-test@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").strip()
