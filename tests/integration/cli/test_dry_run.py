import inspect
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import crewplane.cli.app as cli
from crewplane.version import SCHEMA_VERSION
from tests.helpers.isolated_git import (
    IsolatedGit,
    configure_isolated_git_environment,
    require_git,
)
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    cli_process_state,
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
    @pytest.fixture(autouse=True)
    def _repository_root(self, pytestconfig: pytest.Config) -> None:
        self.repository_root = pytestconfig.rootpath

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
        source_root = (self.repository_root / "src").resolve()
        child_environment = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
            child_environment.pop(name, None)
        child_environment.update(
            {
                "LC_ALL": "C",
                "PYTHONNOUSERSITE": "1",
                "TZ": "UTC",
            }
        )
        probe = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                f"source_root = Path({str(source_root)!r}).resolve()",
                "sys.path.insert(0, str(source_root))",
                "import crewplane",
                "from crewplane.cli.app import app",
                "package_path = Path(crewplane.__file__).resolve()",
                "if not package_path.is_relative_to(source_root):",
                "    raise RuntimeError(f'unexpected crewplane import: {package_path}')",
                "print(type(app).__name__, package_path)",
            )
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            capture_output=True,
            check=False,
            cwd=self.repository_root,
            env=child_environment,
            text=True,
            timeout=30,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                f"command={result.args!r}\n"
                f"cwd={self.repository_root}\n"
                f"stdout={result.stdout}\n"
                f"stderr={result.stderr}"
            ),
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
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            with _isolated_workspace_git() as workspace_git:
                config_path, workflow_path = _write_workspace_enabled_project(tmp_path)
                _commit_workspace_project(tmp_path, workspace_git)

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
        with temporary_project_cwd() as tmp_path:
            with _isolated_workspace_git() as workspace_git:
                config_path, workflow_path = _write_workspace_enabled_project(tmp_path)
                _commit_workspace_project(tmp_path, workspace_git)
                stream = io.StringIO()
                with cli_process_state(stream):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)

            self.assertIn("✓ Valid", stream.getvalue())
            self.assertEqual(artifact_tree(tmp_path / ".crewplane"), ())


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


@contextmanager
def _isolated_workspace_git() -> Iterator[IsolatedGit]:
    with (
        tempfile.TemporaryDirectory(prefix="crewplane-dry-run-git-") as tmp_dir,
        pytest.MonkeyPatch.context() as process_state,
    ):
        environment = configure_isolated_git_environment(process_state, Path(tmp_dir))
        yield require_git(environment, required=False)


def _commit_workspace_project(root: Path, workspace_git: IsolatedGit) -> None:
    workspace_git.run_text(root, "init")
    workspace_git.run_text(root, "config", "user.name", "Crewplane Test")
    workspace_git.run_text(
        root, "config", "user.email", "crewplane-test@example.invalid"
    )
    workspace_git.run_text(root, "add", ".")
    workspace_git.run_text(root, "commit", "-m", "initial")
