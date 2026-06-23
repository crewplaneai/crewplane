from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree.cache import WorktreeReuseCache
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    with_node_setup,
    workspace_request,
)


def test_reused_worktree_setup_runs_after_reset_and_clean(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    nested = repo / "packages" / "app"
    nested.mkdir(parents=True)
    (nested / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    run_git_text(repo, "add", "packages/app/.gitignore")
    run_git_text(repo, "commit", "-m", "ignore caches")
    cache_root = tmp_path / "cache"
    plan = with_node_setup(
        two_node_lineage_plan(repo, cache_root),
        "verify",
        [
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "assert Path('result.txt').read_text() == 'captured\\n'; "
                    "assert not Path('packages/app/.cache').exists(); "
                    "Path('setup-order.txt').write_text('after-reset')"
                ),
            ]
        ],
    )
    output = workspace_output_manager(tmp_path, repo)
    output.create_stage_dir("implement")
    output.create_stage_dir("verify")
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    first = prepare_invocation_workspace(
        workspace_request(plan, output, "implement", reuse_cache, limiter),
        workspace_invocation_context(),
    )
    assert first.workspace_path is not None
    (first.cwd / "result.txt").write_text("captured\n", encoding="utf-8")
    ignored_cache = first.cwd / "packages" / "app" / ".cache" / "tool"
    ignored_cache.mkdir(parents=True)
    (ignored_cache / "leftover.txt").write_text("ignored\n", encoding="utf-8")
    first.mark_succeeded(defer_cleanup=True)
    first.cleanup_after_success()

    second = prepare_invocation_workspace(
        workspace_request(plan, output, "verify", reuse_cache, limiter),
        workspace_invocation_context(),
    )

    try:
        assert (second.cwd / "setup-order.txt").read_text(
            encoding="utf-8"
        ) == "after-reset"
        state = read_json_object(
            output.create_stage_dir("verify") / "workspace-state.json"
        )
        assert state["reuse"]["strategy"] == "incremental_reset"
        assert state["setup"]["status"] == "succeeded"
        assert state["setup"]["commands"][0]["working_directory"] == (
            second.cwd.as_posix()
        )
    finally:
        if second.workspace_path is not None:
            second.mark_succeeded(defer_cleanup=True)
        reuse_cache.cleanup_all_best_effort()
