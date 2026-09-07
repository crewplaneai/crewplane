from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.worktree import remove_worktree_workspace
from crewplane.runtime.workspace.worktree.reset import reset_reusable_worktree_checkout
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


@pytest.mark.parametrize("reset_mode", ["retry", "reuse"])
@pytest.mark.parametrize("index_flag", ["--skip-worktree", "--assume-unchanged"])
def test_worktree_reset_rebuilds_hidden_index_entries(
    tmp_path: Path,
    reset_mode: str,
    index_flag: str,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", False, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, workspace_output_manager(tmp_path, repo)),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    try:
        run_git_text(prepared.cwd, "update-index", index_flag, "README.md")
        (prepared.cwd / "README.md").write_text(
            "failed attempt changes\n", encoding="utf-8"
        )
        if reset_mode == "retry":
            assert prepared.invocation_context.retry_reset is not None
            prepared.invocation_context.retry_reset()
        else:
            assert prepared.worktree_capture is not None
            reset_reusable_worktree_checkout(
                prepared.cwd,
                source.run_base_commit,
                repo,
                Path(source.common_git_dir),
                prepared.worktree_capture.git_dir,
            )

        assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
        assert run_git_text(prepared.cwd, "ls-files", "-v") == "H README.md"
        prepared.mark_succeeded(defer_cleanup=True)
        state = read_json_object(prepared.state_path)
        assert state["result"]["empty_result"] is True
        assert (repo / "README.md").read_text(encoding="utf-8") == "ready\n"
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)


@pytest.mark.parametrize("configured_hooks_path", [False, True])
def test_worktree_materialization_does_not_run_repository_checkout_hooks(
    tmp_path: Path,
    configured_hooks_path: bool,
) -> None:
    repo = create_git_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    if configured_hooks_path:
        hooks = tmp_path / "custom-hooks"
        hooks.mkdir()
        run_git_text(repo, "config", "core.hooksPath", hooks.as_posix())
    hook = hooks / "post-checkout"
    hook.write_text(
        '#!/bin/sh\nprintf "hook changed\\n" > README.md\n', encoding="utf-8"
    )
    hook.chmod(0o755)
    plan = workspace_plan(repo, tmp_path / "cache", False, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    prepared = prepare_invocation_workspace(
        workspace_invocation_request(plan, workspace_output_manager(tmp_path, repo)),
        workspace_invocation_context(),
    )
    assert prepared.workspace_path is not None
    assert prepared.state_path is not None
    try:
        assert (prepared.cwd / "README.md").read_text(encoding="utf-8") == "ready\n"
        prepared.mark_succeeded(defer_cleanup=True)
        state = read_json_object(prepared.state_path)
        assert state["result"]["empty_result"] is True
        assert hook.is_file()
    finally:
        remove_worktree_workspace(source, prepared.workspace_path)
