from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import typer
from rich.console import Console

from crewplane.cli.paths import resolve_state_file, resolve_tasks_file
from crewplane.cli.workflow_context import resolve_cli_workflow_paths
from crewplane.core.state_paths import get_state_dir
from tests.helpers.platforms import symlink_or_skip


def config_file(root: Path) -> Path:
    path = root / ".crewplane/config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"config")
    return path


def config_resolution(override: Path | None = None) -> Path:
    return resolve_state_file(
        override, "config.yml", "Initialize the project.", Console()
    )


def test_discovery_uses_nearest_marker_not_git_or_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = config_file(tmp_path)
    nested = tmp_path / "nested"
    caller = nested / "deep/child"
    caller.mkdir(parents=True)
    (nested / ".git").mkdir()
    (caller.parent / ".crewplane").mkdir()
    monkeypatch.chdir(caller)
    assert config_resolution() == ancestor
    selected = config_file(nested)
    assert config_resolution() == selected
    assert get_state_dir() == caller / ".crewplane"


def test_discovery_terminates_after_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    attempted: list[Path] = []
    stream = io.StringIO()

    def absent(path: Path) -> None:
        attempted.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(Path, "lstat", absent)
    with pytest.raises(typer.Exit):
        resolve_state_file(None, "config.yml", "", Console(file=stream, width=400))
    assert attempted == [
        root / ".crewplane/config.yml" for root in (tmp_path, *tmp_path.parents)
    ]
    assert str(tmp_path) in stream.getvalue()
    assert ".crewplane/config.yml" in stream.getvalue()
    assert "crewplane init" in stream.getvalue()
    assert "--config" in stream.getvalue()


@pytest.mark.parametrize("kind", ["directory", "dangling", "unreadable", "disappeared"])
def test_selected_marker_failure_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    config_file(tmp_path)
    caller = tmp_path / "child"
    selected = config_file(caller)
    if kind == "directory":
        selected.unlink()
        selected.mkdir()
    elif kind == "dangling":
        selected.unlink()
        symlink_or_skip(selected, caller / "absent.yml")
    elif kind == "unreadable":
        original_open = Path.open

        def deny(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == selected:
                raise PermissionError(13, "Permission denied", str(path))
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny)
    else:
        original_lstat = Path.lstat

        def disappear(path: Path) -> Any:
            result = original_lstat(path)
            if path == selected:
                selected.unlink()
            return result

        monkeypatch.setattr(Path, "lstat", disappear)
    monkeypatch.chdir(caller)
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        resolve_state_file(None, "config.yml", "", Console(file=stream, width=400))
    assert str(selected) in stream.getvalue()
    assert Path.cwd() == caller


@pytest.mark.parametrize("failure", ["permission", "not_directory"])
def test_probe_errors_are_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config_file(tmp_path)
    caller = tmp_path / "child"
    caller.mkdir()
    marker = caller / ".crewplane/config.yml"
    if failure == "not_directory":
        (caller / ".crewplane").write_bytes(b"not a directory")
    else:
        original = Path.lstat

        def denied(path: Path) -> Any:
            if path == marker:
                raise PermissionError(13, "Permission denied", str(path))
            return original(path)

        monkeypatch.setattr(Path, "lstat", denied)
    monkeypatch.chdir(caller)
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        resolve_state_file(None, "config.yml", "", Console(file=stream, width=400))
    assert str(marker) in stream.getvalue()


@pytest.mark.parametrize("spelling", ["relative", "absolute", "home"])
def test_explicit_config_uses_invocation_base_and_bypasses_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    config_file(tmp_path)
    caller = tmp_path / "child"
    caller.mkdir()
    target = caller / "custom.yml"
    target.write_bytes(b"custom")
    monkeypatch.chdir(caller)
    monkeypatch.setenv("HOME", str(caller))
    monkeypatch.setenv("USERPROFILE", str(caller))
    override = {
        "relative": Path("custom.yml"),
        "absolute": target,
        "home": Path("~/custom.yml"),
    }[spelling]
    assert config_resolution(override) == target
    target.unlink()
    with pytest.raises(typer.Exit):
        config_resolution(override)


def test_config_symlink_and_invocation_symlink_use_canonical_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    selected = config_file(source)
    workflows = source / ".crewplane/workflows"
    workflows.mkdir()
    workflow = workflows / "one.task.md"
    workflow.write_bytes(b"workflow")
    caller = tmp_path / "caller"
    link = config_file(caller)
    link.unlink()
    symlink_or_skip(link, selected)
    monkeypatch.chdir(caller)
    paths = resolve_cli_workflow_paths(None, None, Console())
    assert (paths.config, paths.tasks, paths.project_root, paths.state_dir) == (
        selected,
        workflow,
        source,
        source / ".crewplane",
    )
    nested = source / "sub/child"
    nested.mkdir(parents=True)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, nested, target_is_directory=True)
    monkeypatch.chdir(alias)
    assert resolve_cli_workflow_paths(None, None, Console()) == paths


def test_config_outside_state_uses_its_parent_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "custom.yml"
    config.write_bytes(b"config")
    workflow = tmp_path / "workflow.task.md"
    workflow.write_bytes(b"workflow")
    caller = tmp_path / "child"
    caller.mkdir()
    monkeypatch.chdir(caller)
    paths = resolve_cli_workflow_paths(Path("workflow.task.md"), config, Console())
    assert paths.project_root == tmp_path
    assert paths.state_dir == tmp_path / ".crewplane"
    assert paths.tasks == workflow


@pytest.mark.parametrize("kind", ["none", "many", "directory"])
def test_default_workflow_rejects_invalid_candidates(tmp_path: Path, kind: str) -> None:
    workflows = tmp_path / ".crewplane/workflows"
    examples = workflows / "examples"
    examples.mkdir(parents=True)
    (examples / "nested.task.md").write_bytes(b"ignored")
    if kind == "many":
        for name in ("one.task.md", "two.task.md"):
            (workflows / name).write_bytes(b"workflow")
    if kind == "directory":
        (workflows / "one.task.md").mkdir()
    stream = io.StringIO()
    with pytest.raises(typer.Exit):
        resolve_tasks_file(None, "", Console(file=stream, width=400), tmp_path)
    assert str(workflows) in stream.getvalue()


def test_invocation_directory_failure_is_a_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> Path:
        raise FileNotFoundError("Invocation directory no longer exists")

    monkeypatch.setattr(Path, "cwd", missing)
    with pytest.raises(typer.Exit):
        config_resolution()
