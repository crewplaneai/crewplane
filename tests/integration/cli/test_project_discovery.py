from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import crewplane.cli.app as cli
import crewplane.cli.workflow_context as workflow_context
from crewplane.cli.workflow_context import (
    compile_preview_for_context,
    load_cli_workflow_context,
    resolve_cli_workflow_paths,
)
from tests.helpers.mock_resume import write_executor_fixture, write_mock_config
from tests.integration.cli.cli_workflow_helpers import write_basic_workflow
from tests.integration.cli.dry_run_helpers import write_standard_project
from tests.integration.cli.repeat_force_run_support import filesystem_snapshot


def project(root: Path) -> tuple[Path, Path, Path]:
    config, workflow = write_standard_project(root)
    write_mock_config(config, Path("fixtures"))
    write_executor_fixture(root / "fixtures", "review.node", "completed")
    caller = root / "backend/api"
    caller.mkdir(parents=True)
    return config, workflow, caller


def command_args(command: str, operand: str | None = None) -> list[str]:
    if command == "validate":
        return ["validate", *([] if operand is None else [operand])]
    return [
        "run",
        *([] if operand is None else ["--tasks", operand]),
        "--dry-run" if command == "dry-run" else "--no-live",
    ]


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
@pytest.mark.parametrize(
    "spelling", ["omitted", "relative", "dot", "parent", "absolute", "home", "external"]
)
def test_public_commands_rebase_workflows_after_project_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, spelling: str
) -> None:
    root = tmp_path / "project"
    _, workflow, caller = project(root)
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    relative = str(workflow.relative_to(root))
    operand = {
        "omitted": None,
        "relative": relative,
        "dot": f"./{relative}",
        "parent": f"../project/{relative}",
        "absolute": str(workflow),
        "home": f"~/project/{relative}",
        "external": str(tmp_path / "external.task.md"),
    }[spelling]
    if spelling == "external":
        workflow = tmp_path / "external.task.md"
        write_basic_workflow(workflow)
    before = filesystem_snapshot(root)
    monkeypatch.chdir(caller)
    result = CliRunner().invoke(cli.app, command_args(command, operand))
    assert result.exit_code == 0, (result.output, result.exception)
    displayed = (
        str(workflow.relative_to(root))
        if workflow.is_relative_to(root)
        else str(workflow)
    )
    assert (
        f"Crewplane project: {root}\nConfig: {Path('.crewplane/config.yml')}\n"
        f"Workflow: {displayed}" in result.output
    )
    assert result.output.count("Crewplane project:") == 1
    assert result.output.index("Workflow:") < result.output.index(
        "Validating"
        if command == "validate"
        else "Dry run mode"
        if command == "dry-run"
        else "Mock invoker active"
    )
    assert Path.cwd() == caller
    assert not (caller / ".crewplane").exists()
    if command != "run":
        assert filesystem_snapshot(root) == before


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
@pytest.mark.parametrize("decoy", ["file", "directory", "unreadable"])
def test_caller_workflow_collision_does_not_override_project_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, decoy: str
) -> None:
    _, _, caller = project(tmp_path)
    write_basic_workflow(tmp_path / "review.task.md")
    local = caller / "review.task.md"
    local.mkdir() if decoy == "directory" else local.write_bytes(b"invalid workflow")
    if decoy == "unreadable":
        original_access = os.access

        def access(path: Any, mode: int, **kwargs: Any) -> bool:
            if Path(path).resolve() == local and mode == os.R_OK:
                return False
            return original_access(path, mode, **kwargs)

        monkeypatch.setattr(os, "access", access)
    monkeypatch.chdir(caller)
    result = CliRunner().invoke(cli.app, command_args(command, "review.task.md"))
    assert result.exit_code == 0, (result.output, result.exception)
    assert "Workflow: review.task.md" in result.output
    assert Path.cwd() == caller
    assert not (caller / ".crewplane").exists()


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
@pytest.mark.parametrize("failure", ["missing", "directory", "unreadable"])
def test_selected_workflow_failure_has_no_caller_fallback_or_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, failure: str
) -> None:
    _, _, caller = project(tmp_path)
    workflow = tmp_path / "review.task.md"
    write_basic_workflow(caller / workflow.name)
    if failure == "directory":
        workflow.mkdir()
    elif failure == "unreadable":
        write_basic_workflow(workflow)
        original = Path.open

        def denied(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == workflow:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.chdir(caller)
    result = CliRunner().invoke(cli.app, command_args(command, workflow.name))
    assert result.exit_code == 1, (result.output, result.exception)
    assert str(workflow) in result.output
    assert "Crewplane project:" not in result.output
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    assert not (caller / ".crewplane").exists()
    assert Path.cwd() == caller


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
@pytest.mark.parametrize("failure", ["yaml", "schema", "workflow"])
def test_selected_project_load_failure_never_uses_valid_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, failure: str
) -> None:
    project(tmp_path)
    selected = tmp_path / "nested"
    config, workflow, caller = project(selected)
    if failure == "yaml":
        config.write_bytes(b"version: [broken")
    elif failure == "schema":
        config.write_bytes(b"version: invalid\nagents: {}\n")
    else:
        workflow.write_bytes(b"not a workflow")
    monkeypatch.chdir(caller)
    monkeypatch.setenv("COLUMNS", "400")
    result = CliRunner().invoke(cli.app, command_args(command))
    assert result.exit_code == 1, (result.output, result.exception)
    assert f"Crewplane project: {selected}" in result.output
    assert f"Config: {Path('.crewplane/config.yml')}" in result.output
    if failure == "workflow":
        assert str(workflow) in result.output
    diagnostics = list(
        (selected / ".crewplane/execution-stages").glob("*/preflight/diagnostics.json")
    )
    assert bool(diagnostics) is (command == "run")
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    assert not (caller / ".crewplane").exists()
    assert Path.cwd() == caller


@pytest.mark.parametrize("failure", ["absent", "probe", "root", "marker"])
def test_run_path_selection_failures_allocate_no_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    if failure != "absent":
        config, _ = write_standard_project(tmp_path)
        if failure == "marker":
            config.unlink()
            config.mkdir()
        elif failure == "root":

            def failed_root(path: Path) -> Path:
                raise OSError(f"Cannot resolve project for {path}")

            monkeypatch.setattr(
                workflow_context, "project_root_from_config_path", failed_root
            )
        else:
            original = Path.lstat

            def denied(path: Path) -> Any:
                if path == caller / ".crewplane/config.yml":
                    raise PermissionError(13, "Permission denied", str(path))
                return original(path)

            monkeypatch.setattr(Path, "lstat", denied)
    monkeypatch.chdir(caller)
    before = filesystem_snapshot(tmp_path)
    result = CliRunner().invoke(cli.app, ["run", "--no-live"])
    assert result.exit_code == 1, (result.output, result.exception)
    assert "Crewplane project:" not in result.output
    assert filesystem_snapshot(tmp_path) == before
    assert Path.cwd() == caller


def test_context_paths_are_literal_and_default_helpers_are_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "[red]project"
    config, workflow, caller = project(root)
    renamed = workflow.with_name("[bold]review.task.md")
    workflow.rename(renamed)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("COLUMNS", "400")
    stream = io.StringIO()
    paths = resolve_cli_workflow_paths(None, None, Console(file=stream))
    assert (paths.tasks, paths.config) == (renamed, config)
    assert not stream.getvalue()
    result = CliRunner().invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert f"Crewplane project: {root}" in result.output
    assert f"Workflow: {renamed.relative_to(root)}" in result.output


def test_root_and_descendant_compile_identical_resources_and_identity_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    config, workflow, caller = project(root)
    external = tmp_path / "shared"
    external.mkdir()
    (external / "source.txt").write_bytes(b"external source\n")
    (root / "context.txt").write_bytes(b"project source\n")
    write_basic_workflow(
        workflow,
        "{{var:project_name}} {{file:context.txt}} {{file:../shared/source.txt}}",
    )
    payload = yaml.safe_load(config.read_bytes())
    payload["settings"]["file_access"] = {"allowed_template_paths": ["../shared"]}
    config.write_text(yaml.safe_dump(payload), encoding="utf-8", newline="\n")
    before = filesystem_snapshot(tmp_path)
    selected = []
    previews = []
    for cwd in (root, caller):
        monkeypatch.chdir(cwd)
        paths = resolve_cli_workflow_paths(None, None, Console(file=io.StringIO()))
        context = load_cli_workflow_context(paths)
        selected.append(paths)
        previews.append(
            compile_preview_for_context(context, True, Console(file=io.StringIO()))
        )
        for command in ("validate", "dry-run"):
            result = CliRunner().invoke(cli.app, command_args(command))
            assert result.exit_code == 0, (result.output, result.exception)
        assert Path.cwd() == cwd
    assert selected[0] == selected[1]
    first, second = previews
    assert first.workflow_signature == second.workflow_signature
    assert (
        first.effective_runtime_config_signature
        == second.effective_runtime_config_signature
    )
    assert first.runtime_config_snapshot == second.runtime_config_snapshot
    assert first.static_resources == second.static_resources
    assert first.static_file_payloads == second.static_file_payloads
    assert set(first.static_file_payloads.values()) == {
        b"external source\n",
        b"project source\n",
    }
    assert first.render_plans == second.render_plans
    project_names = [
        fragment.value_stored
        for plan in first.render_plans
        for stream in plan.streams
        for fragment in stream.fragments
        if fragment.kind == "static_var" and fragment.key == "project_name"
    ]
    assert set(project_names) == {root.name}
    assert filesystem_snapshot(tmp_path) == before


def test_global_help_needs_no_project_and_produces_no_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Crewplane project:" not in result.output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["zero", "multiple", "directory"])
def test_nearer_project_does_not_use_parent_default_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    project(tmp_path)
    nested = tmp_path / "nested"
    _, workflow, caller = project(nested)
    if kind == "multiple":
        write_basic_workflow(workflow.with_name("second.task.md"))
    else:
        workflow.unlink()
        if kind == "directory":
            workflow.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setenv("COLUMNS", "400")
    result = CliRunner().invoke(cli.app, ["run", "--no-live"])
    assert result.exit_code == 1, result.output
    assert str(workflow.parent) in result.output
    assert not (nested / ".crewplane/execution-stages").exists()
    assert not (caller / ".crewplane").exists()


@pytest.mark.parametrize("command", ["validate", "dry-run", "run"])
def test_invalid_explicit_config_never_substitutes_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _, workflow, caller = project(tmp_path)
    override = caller / "custom.yml"
    override.write_bytes(b"version: invalid\nagents: {}\n")
    monkeypatch.chdir(caller)
    monkeypatch.setenv("COLUMNS", "400")
    result = CliRunner().invoke(
        cli.app,
        [*command_args(command, str(workflow)), "--config", "custom.yml"],
    )
    assert result.exit_code == 1, result.output
    assert f"Crewplane project: {caller}\nConfig: custom.yml" in result.output
    assert "Unsupported config version" in result.output
    assert not (tmp_path / ".crewplane/execution-stages").exists()
    assert bool(
        list(
            (caller / ".crewplane/execution-stages").glob(
                "*/preflight/diagnostics.json"
            )
        )
    ) is (command == "run")
