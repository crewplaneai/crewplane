import io
import unittest
from pathlib import Path

import pytest
import typer
import yaml

import crewplane.cli.app as cli
from crewplane.version import SCHEMA_VERSION
from tests.helpers.working_directory import temporary_project_cwd
from tests.integration.cli.cli_workflow_helpers import (
    ConsoleFactory,
    write_basic_config,
    write_basic_workflow,
)


class CliValidateImportsAndSchemaTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def temporary_directory_root(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path

    def test_validate_accepts_workflow_markdown(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
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
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
            try:
                cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Imports: 1 imported workflow file(s) resolved", output_text)
            self.assertIn("Valid:", output_text)

    def test_validate_rejects_unused_import_parameter(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Invalid:", output_text)
            self.assertIn("unused parameter", " ".join(output_text.split()))

    def test_validate_fails_fast_for_unknown_provider(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Provider validation failed", output_text)
            self.assertIn("Unknown provider 'missing'", output_text)
            self.assertNotIn("Invalid:", output_text)

    def test_validate_surfaces_strict_workflow_schema_errors(self) -> None:
        with temporary_project_cwd(self.tmp_path) as tmp_path:
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
            try:
                with self.assertRaises(typer.Exit):
                    cli.validate(tasks_file=workflow_path, config_file=config_path)
            finally:
                cli.Console = original_console_cls

            output_text = stream.getvalue()
            self.assertIn("Invalid:", output_text)
            self.assertIn("Extra inputs are not permitted", output_text)


@pytest.mark.parametrize("command", [["validate"], ["run", "--dry-run"]])
@pytest.mark.parametrize(
    "resource", ["valid", "allowlisted", "symlink-escape", "reserved"]
)
def test_descendant_imported_resources_keep_project_base_and_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str], resource: str
) -> None:
    from typer.testing import CliRunner

    from tests.helpers.foreach import workflow_document, write_workflow_document
    from tests.helpers.import_needs import write_import_needs_workflow
    from tests.helpers.platforms import symlink_or_skip
    from tests.integration.cli.cli_workflow_helpers import write_mock_config
    from tests.integration.cli.repeat_force_run_support import filesystem_snapshot

    project = tmp_path / "project"
    child = project / "backend/api"
    child.mkdir(parents=True)
    workflow = write_import_needs_workflow(project)
    imported = workflow.parent / "child.task.md"
    payload, body = workflow_document(imported)
    write_workflow_document(imported, payload, body + "\nRead {{file:context.md}}.\n")
    write_mock_config(project / ".crewplane/config.yml")
    if resource in {"allowlisted", "symlink-escape"}:
        outside = tmp_path / "outside.md"
        outside.write_bytes(b"not authorized\n")
        (project / "context.md").unlink()
        symlink_or_skip(project / "context.md", outside)
        if resource == "allowlisted":
            config_path = project / ".crewplane/config.yml"
            config = yaml.safe_load(config_path.read_bytes())
            config["settings"]["file_access"] = {
                "allowed_template_paths": ["../outside.md"]
            }
            config_path.write_text(
                yaml.safe_dump(config), encoding="utf-8", newline="\n"
            )
    elif resource == "reserved":
        reserved = project / ".crewplane/execution-stages/context.md"
        reserved.parent.mkdir(parents=True)
        reserved.write_bytes(b"runtime evidence\n")
        (project / "context.md").unlink()
        symlink_or_skip(project / "context.md", reserved)
    monkeypatch.chdir(child)
    before = filesystem_snapshot(project)
    operand = str(workflow.relative_to(project))
    arguments = [
        *command,
        *([operand] if command == ["validate"] else ["--tasks", operand]),
    ]
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == (0 if resource in {"valid", "allowlisted"} else 1), (
        result.output
    )
    assert filesystem_snapshot(project) == before
    assert Path.cwd() == child


def test_descendant_foreach_keeps_declaring_file_relative_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from tests.helpers.foreach import copy_foreach_fixture
    from tests.integration.cli.cli_workflow_helpers import write_mock_config
    from tests.integration.cli.repeat_force_run_support import filesystem_snapshot

    workflow = copy_foreach_fixture(tmp_path)
    (tmp_path / ".crewplane").mkdir()
    write_mock_config(tmp_path / ".crewplane/config.yml")
    child = tmp_path / "backend/api"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    config_path = tmp_path / ".crewplane/config.yml"
    config = yaml.safe_load(config_path.read_bytes())
    config["settings"]["max_concurrent_nodes"] = 1
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8", newline="\n")
    before = filesystem_snapshot(tmp_path)
    result = CliRunner().invoke(cli.app, ["validate", workflow.name])
    assert result.exit_code == 0, result.output
    assert filesystem_snapshot(tmp_path) == before
    assert not (child / ".crewplane").exists()


@pytest.mark.parametrize("reference", ["import", "foreach"])
def test_descendant_composition_still_rejects_project_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    from typer.testing import CliRunner

    from tests.helpers.foreach import (
        copy_foreach_fixture,
        workflow_document,
        write_workflow_document,
    )
    from tests.integration.cli.cli_workflow_helpers import write_mock_config

    project = tmp_path / "project"
    workflow = copy_foreach_fixture(project)
    outside = tmp_path / "outside.md"
    write_basic_workflow(outside)
    payload, body = workflow_document(workflow)
    if reference == "import":
        payload["imports"] = [{"path": "../outside.md", "as": "outside"}]
    else:
        payload["nodes"][2]["workflow"] = "../outside.md"
    write_workflow_document(workflow, payload, body)
    (project / ".crewplane").mkdir()
    write_mock_config(project / ".crewplane/config.yml")
    child = project / "backend/api"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
    result = CliRunner().invoke(cli.app, ["validate", workflow.name])
    assert result.exit_code == 1, result.output
    assert "outside" in result.output.lower(), result.output
    assert not (child / ".crewplane").exists()
