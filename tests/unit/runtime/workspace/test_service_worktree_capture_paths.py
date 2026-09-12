from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import crewplane.runtime.workspace.worktree.orchestration as workspace_worktree
from crewplane.runtime.workspace import (
    prepare_invocation_workspace,
)
from crewplane.runtime.workspace.filesystem import (
    remove_workspace_path,
)
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_worktree_capture_rejects_replaced_workspace_root_symlink(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    outside = tmp_path / "outside"
    outside.mkdir()
    external_checkout = outside / "checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        _remove_git_worktree_best_effort(repo, external_checkout)
        pytest.skip("symlink creation is unavailable")

    try:
        with pytest.raises(RuntimeError, match="Workspace capture root"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


def test_worktree_capture_reports_unsafe_root_before_checkout_mismatch(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.worktree_capture is not None
    source = plan.workspace_source
    assert source is not None
    invalid_request = replace(
        prepared.worktree_capture,
        workspace_path=tmp_path / "missing-workspace",
        checkout_root=tmp_path / "unrelated-checkout",
    )

    with pytest.raises(RuntimeError, match="Workspace capture root is missing"):
        workspace_worktree.capture_worktree_result(invalid_request)

    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_replaced_checkout_symlink(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    external_checkout = tmp_path / "external-checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    workspace_path.mkdir(parents=True)
    try:
        (workspace_path / "checkout").symlink_to(
            external_checkout,
            target_is_directory=True,
        )
    except OSError:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)
        pytest.skip("symlink creation is unavailable")

    try:
        with pytest.raises(RuntimeError, match="Workspace capture checkout"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


def test_worktree_capture_rejects_checkout_gitdir_for_external_worktree(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    workspace_path = prepared.workspace_path
    checkout_root = workspace_path / "checkout"
    external_checkout = tmp_path / "external-checkout"
    run_git_text(
        repo,
        "worktree",
        "add",
        "--detach",
        external_checkout.as_posix(),
        source.run_base_commit,
    )
    remove_worktree_workspace(source, workspace_path)
    checkout_root.mkdir(parents=True)
    (checkout_root / ".git").write_text(
        (external_checkout / ".git").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    try:
        with pytest.raises(RuntimeError, match="Git dir changed after materialization"):
            prepared.mark_succeeded()
    finally:
        remove_workspace_path(workspace_path)
        _remove_git_worktree_best_effort(repo, external_checkout)


def _remove_git_worktree_best_effort(repo: Path, checkout: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            repo.as_posix(),
            "worktree",
            "remove",
            "--force",
            "--force",
            checkout.as_posix(),
        ],
        check=False,
        capture_output=True,
    )
