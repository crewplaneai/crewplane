from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.preflight import (
    PreflightCompileOptions,
    PreflightWorkflowSource,
    compile_preflight_preview,
)
from crewplane.core.preflight.secrets import FingerprintKeyProvider
from crewplane.core.preflight.static_resources import resolve_static_file
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.workspace_preflight import (
    compile_workflow_with_source_snapshot,
    init_git_repo,
    workspace_config,
    workspace_workflow,
)

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize(
    ("raw_path", "message"),
    [
        ("file\x00.md", "must not contain NUL bytes"),
        ("/unlisted/context.md", "must be explicitly allowlisted"),
        ("../context.md", "escapes the project root"),
        ("nested/../..", "escapes the project root"),
        ("nested/..", "escapes the project root"),
    ],
)
@pytest.mark.usefixtures("isolated_git")
def test_workspace_file_preflight_rejects_unsafe_paths(
    tmp_path: Path, raw_path: str, message: str
) -> None:
    (tmp_path / "context.md").write_text("Project context", encoding="utf-8")
    snapshot = init_git_repo(tmp_path)

    preview = compile_workflow_with_source_snapshot(
        tmp_path, workspace_workflow(f"Read {{{{file:{raw_path}}}}}"), snapshot
    )

    assert preview.workflow_signature is None
    assert preview.workspace_file_payloads == {}
    assert any(message in diagnostic.message for diagnostic in preview.diagnostics)
    assert {diagnostic.code for diagnostic in preview.diagnostics} == {
        "WORKSPACE-FILE-LOCATOR"
    }


@pytest.mark.parametrize("operation", ["ls-tree", "cat-file"])
@pytest.mark.usefixtures("isolated_git")
def test_workspace_file_preflight_reports_git_read_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    (tmp_path / "context.md").write_text("Project context", encoding="utf-8")
    snapshot = init_git_repo(tmp_path)
    original_run = subprocess.run
    attempted: list[list[str]] = []

    def run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess[Any]:
        if operation in arguments:
            attempted.append(arguments)
            raise subprocess.CalledProcessError(
                128, arguments, stderr=b"object database unavailable"
            )
        return original_run(arguments, **options)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", run)
        preview = compile_workflow_with_source_snapshot(
            tmp_path, workspace_workflow("Read {{file:context.md}}"), snapshot
        )

    assert attempted
    assert preview.workflow_signature is None
    assert preview.workspace_file_payloads == {}
    assert any(
        "object database unavailable" in diagnostic.message
        for diagnostic in preview.diagnostics
    )
    assert {diagnostic.code for diagnostic in preview.diagnostics} == {
        "WORKSPACE-FILE-LOCATOR"
    }


@pytest.mark.parametrize("destination", ["external", "runtime-owned"])
def test_static_file_policy_rechecks_symlink_destination_after_existence_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = project / "context.md"
    path.write_text("Safe original", encoding="utf-8")
    target = (
        tmp_path / "external.md"
        if destination == "external"
        else project / ".crewplane" / "execution-stages" / "secret.md"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Do not read", encoding="utf-8")
    original_exists = Path.exists
    replaced = False

    def exists(candidate: Path) -> bool:
        nonlocal replaced
        if candidate == path and not replaced:
            replaced = True
            path.unlink()
            path.symlink_to(target)
        return original_exists(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", exists)
        result = resolve_static_file("context.md", project, project, ())

    assert replaced
    assert result.resource is None
    assert result.payload is None
    assert len(result.diagnostics) == 1
    expected = (
        "after symlink resolution"
        if destination == "external"
        else "runtime-owned path"
    )
    assert expected in result.diagnostics[0].message


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        (" ", None, "path is empty"),
        ("directory", None, "Not a file"),
        ("context.md", b"text\x00text", "File contains NUL bytes"),
    ],
)
def test_static_file_rejections_do_not_materialize_payload(
    tmp_path: Path, path: str, content: bytes | None, message: str
) -> None:
    if content is not None:
        (tmp_path / path).write_bytes(content)
    elif path == "directory":
        (tmp_path / path).mkdir()

    result = resolve_static_file(path, tmp_path, tmp_path, ())

    assert result.resource is None
    assert result.payload is None
    assert len(result.diagnostics) == 1
    assert message in result.diagnostics[0].message


@pytest.mark.parametrize("secret_location", ["environment", "config"])
def test_corrupt_fingerprint_key_stops_preflight_without_persisting_secrets(
    tmp_path: Path, secret_location: str
) -> None:
    key_path = FingerprintKeyProvider(tmp_path / ".crewplane").key_path
    key_path.parent.mkdir(parents=True)
    key_path.write_bytes(b"short")
    key_path.chmod(0o600)
    config = workspace_config({"enabled": False})
    secret = "test-only-private-value"
    prompt = "Use {{env:API_TOKEN}}" if secret_location == "environment" else "Run"
    workflow = workspace_workflow(prompt)
    workflow.worktrees = {}
    if secret_location == "config":
        config.agents["alpha"].extra_args = ["--api-token", secret]
    runtime = build_runtime_config_snapshot(
        config=config, console=Console(file=None), no_live=True
    )

    preview = compile_preflight_preview(
        source=PreflightWorkflowSource.from_workflow(workflow),
        config=config,
        runtime_snapshot=runtime.snapshot,
        options=PreflightCompileOptions(
            project_root=tmp_path,
            state_dir=tmp_path / ".crewplane",
            environment={"API_TOKEN": secret},
            fingerprint_key_policy="persist_if_needed",
        ),
    )

    assert preview.workflow_signature is None
    assert {diagnostic.code for diagnostic in preview.diagnostics} == {
        "FINGERPRINT-KEY"
    }
    assert secret not in preview.model_dump_json()
    assert key_path.read_bytes() == b"short"
