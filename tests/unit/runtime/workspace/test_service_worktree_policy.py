from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.workspace_service import (
    create_git_repo,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


@pytest.mark.parametrize(
    ("policy_file", "content", "message"),
    [
        ("attributes", "* text=auto\n", "common Git info/attributes"),
        ("exclude", "*.tmp\n", "common Git info/exclude"),
    ],
)
def test_worktree_capture_rejects_common_policy_file_drift(
    tmp_path: Path,
    policy_file: str,
    content: str,
    message: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    drift_path = Path(source.common_git_dir) / "info" / policy_file
    drift_path.write_text(content, encoding="utf-8")

    try:
        with pytest.raises(RuntimeError, match=message):
            prepared.mark_succeeded()
    finally:
        drift_path.unlink(missing_ok=True)
        remove_worktree_workspace(source, prepared.workspace_path)


@pytest.mark.parametrize(
    ("drift_kind", "message"),
    [
        ("alternates", "object alternates"),
        ("grafts", "grafts"),
        ("replacement_refs", "replacement refs"),
    ],
)
def test_worktree_capture_rejects_common_object_behavior_drift(
    tmp_path: Path,
    drift_kind: str,
    message: str,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    common_git_dir = Path(source.common_git_dir)
    drift_paths: list[Path] = []
    if drift_kind == "alternates":
        drift_path = common_git_dir / "objects" / "info" / "alternates"
        drift_path.parent.mkdir(parents=True, exist_ok=True)
        drift_path.write_text(tmp_path.as_posix(), encoding="utf-8")
        drift_paths.append(drift_path)
    elif drift_kind == "grafts":
        drift_path = common_git_dir / "info" / "grafts"
        drift_path.parent.mkdir(parents=True, exist_ok=True)
        drift_path.write_text(f"{source.run_base_commit}\n", encoding="utf-8")
        drift_paths.append(drift_path)
    else:
        replace_root = common_git_dir / "refs" / "replace"
        replace_root.mkdir(parents=True, exist_ok=True)
        drift_path = replace_root / source.run_base_commit
        drift_path.write_text(f"{source.run_base_commit}\n", encoding="utf-8")
        drift_paths.append(drift_path)

    try:
        with pytest.raises(RuntimeError, match=message):
            prepared.mark_succeeded()
    finally:
        for drift_path in drift_paths:
            drift_path.unlink(missing_ok=True)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_sibling_result_ref_updates(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    sibling_ref = (
        "refs/crewplane/runs/workspace-run-001/sibling/sibling-alpha-round1/result"
    )
    run_git_text(prepared.cwd, "update-ref", sibling_ref, source.run_base_commit)

    try:
        with pytest.raises(RuntimeError, match="protected crewplane Git refs"):
            prepared.mark_succeeded()
    finally:
        run_git_text(prepared.cwd, "update-ref", "-d", sibling_ref)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_allows_unprotected_branch_ref_updates(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    user_ref = "refs/heads/provider-created"
    run_git_text(prepared.cwd, "update-ref", user_ref, source.run_base_commit)

    try:
        prepared.mark_succeeded()
    finally:
        run_git_text(prepared.cwd, "update-ref", "-d", user_ref)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_ignored_gitattributes_drift(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    run_git_text(repo, "add", ".gitignore")
    run_git_text(repo, "commit", "-m", "ignore directory")
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    ignored_dir = prepared.cwd / "ignored"
    ignored_dir.mkdir()
    (ignored_dir / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=".gitattributes"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_gitignore_that_hides_result_files(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / ".gitignore").write_text("hidden-result.txt\n", encoding="utf-8")
    (prepared.cwd / "hidden-result.txt").write_text("captured\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=".gitignore"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_attributes_for_new_files(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.txt text=auto\n", encoding="utf-8")
    run_git_text(repo, "add", ".gitattributes")
    run_git_text(repo, "commit", "-m", "attributes")
    cache_root = tmp_path / "cache"
    plan = workspace_plan(
        repo,
        cache_root,
        cleanup_on_success=False,
        kind="worktree",
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")

    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, output),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    source = plan.workspace_source
    assert source is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="byte-transforming Git attributes"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)
