from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree import policy as worktree_policy
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from tests.helpers.workspace_service import (
    create_git_repo,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


def test_worktree_retry_reset_rejects_own_protected_ref_drift(
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
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    assert prepared.worktree_capture is not None
    run_git_text(
        prepared.cwd,
        "update-ref",
        (
            "refs/crewplane/runs/workspace-run-001/implement/"
            f"{prepared.worktree_capture.slug}/candidate"
        ),
        source.run_base_commit,
    )

    with pytest.raises(RuntimeError, match="protected crewplane Git refs"):
        prepared.invocation_context.retry_reset()

    run_git_text(
        prepared.cwd,
        "update-ref",
        "-d",
        (
            "refs/crewplane/runs/workspace-run-001/implement/"
            f"{prepared.worktree_capture.slug}/candidate"
        ),
    )
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_rejects_sibling_protected_ref_drift(
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
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    sibling_ref = "refs/crewplane/runs/workspace-run-001/other-node/other-slug/result"
    run_git_text(prepared.cwd, "update-ref", sibling_ref, source.run_base_commit)

    try:
        with pytest.raises(RuntimeError, match="protected crewplane Git refs"):
            prepared.invocation_context.retry_reset()
    finally:
        run_git_text(prepared.cwd, "update-ref", "-d", sibling_ref)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_sibling_protected_ref_drift(
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
    sibling_ref = "refs/crewplane/runs/workspace-run-001/other-node/other-slug/result"
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    run_git_text(prepared.cwd, "update-ref", sibling_ref, source.run_base_commit)

    try:
        with pytest.raises(RuntimeError, match="protected crewplane Git refs"):
            prepared.mark_succeeded()
    finally:
        run_git_text(prepared.cwd, "update-ref", "-d", sibling_ref)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_allows_unprotected_user_branch_updates(
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
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    user_ref = "refs/heads/provider-created"
    run_git_text(prepared.cwd, "update-ref", user_ref, source.run_base_commit)

    try:
        prepared.invocation_context.retry_reset()
    finally:
        run_git_text(prepared.cwd, "update-ref", "-d", user_ref)
        remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_retry_reset_rejects_common_config_drift(
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
    assert prepared.invocation_context.retry_reset is not None
    source = plan.workspace_source
    assert source is not None
    run_git_text(repo, "config", "core.attributesFile", "/tmp/attributes")

    try:
        with pytest.raises(RuntimeError, match="common Git config"):
            prepared.invocation_context.retry_reset()
    finally:
        run_git_text(repo, "config", "--unset", "core.attributesFile")
        remove_worktree_workspace(source, prepared.workspace_path)


def test_common_git_config_rejected_keys_inspects_local_config_without_includes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_args: tuple[str, ...] = ()

    class FakeGitCommand:
        def zero_records(self, *args: str) -> tuple[str, ...]:
            nonlocal captured_args
            captured_args = args
            return ("local", "file:.git/config", "core.attributesFile")

    def fake_git(cwd: Path) -> FakeGitCommand:
        assert cwd == tmp_path
        return FakeGitCommand()

    monkeypatch.setattr(worktree_policy, "git", fake_git)

    assert worktree_policy.common_git_config_rejected_keys(tmp_path) == (
        "core.attributesfile",
    )
    assert "--no-includes" in captured_args
    assert "--show-origin" in captured_args
    assert "--show-scope" in captured_args


def test_worktree_capture_rejects_own_protected_ref_drift(
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
    assert prepared.worktree_capture is not None
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    run_git_text(
        prepared.cwd,
        "update-ref",
        (
            "refs/crewplane/runs/workspace-run-001/implement/"
            f"{prepared.worktree_capture.slug}/candidate"
        ),
        source.run_base_commit,
    )

    with pytest.raises(RuntimeError, match="protected crewplane Git refs"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    run_git_text(
        prepared.cwd,
        "update-ref",
        "-d",
        (
            "refs/crewplane/runs/workspace-run-001/implement/"
            f"{prepared.worktree_capture.slug}/candidate"
        ),
    )
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_branch_attachment_without_head_movement(
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
    run_git_text(prepared.cwd, "checkout", "-b", "provider-branch")
    (prepared.cwd / "result.txt").write_text("captured\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="detached from branches"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_case_colliding_result_paths(
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
    (prepared.cwd / "Case.txt").write_text("upper\n", encoding="utf-8")
    (prepared.cwd / "case.txt").write_text("lower\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="case or Unicode normalization"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_unicode_colliding_result_paths(
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
    (prepared.cwd / "Cafe\u0301.txt").write_text("decomposed\n", encoding="utf-8")
    (prepared.cwd / "Café.txt").write_text("composed\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="case or Unicode normalization"):
        prepared.mark_succeeded()

    assert prepared.workspace_path.exists()
    remove_worktree_workspace(source, prepared.workspace_path)


def test_worktree_capture_rejects_common_config_drift(
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
    run_git_text(repo, "config", "core.attributesFile", "/tmp/attributes")

    try:
        with pytest.raises(RuntimeError, match="common Git config"):
            prepared.mark_succeeded()
    finally:
        run_git_text(repo, "config", "--unset", "core.attributesFile")
        remove_worktree_workspace(source, prepared.workspace_path)
