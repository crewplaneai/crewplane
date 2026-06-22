from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from orchestrator_cli.runtime.workspace import prepare_invocation_workspace
from orchestrator_cli.runtime.workspace.service import MaterializationLimiter
from orchestrator_cli.runtime.workspace.worktree.cache import WorktreeReuseCache
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    run_git_text,
    workspace_invocation_context,
    workspace_output_manager,
)
from tests.helpers.workspace_worktree_reuse import (
    three_node_lineage_plan,
    workspace_request,
)


def test_three_node_same_worktree_chain_emits_additive_state_and_bundles(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    repo = create_git_repo(tmp_path)
    cache_root = tmp_path / "cache"
    plan = three_node_lineage_plan(repo, cache_root)
    output = workspace_output_manager(tmp_path, repo)
    for node_id in ("implement", "verify", "finalize"):
        output.create_stage_dir(node_id)
    reuse_cache = WorktreeReuseCache()
    limiter = MaterializationLimiter.from_plan(plan)

    for node_id in ("implement", "verify", "finalize"):
        prepared = prepare_invocation_workspace(
            workspace_request(plan, output, node_id, reuse_cache, limiter),
            workspace_invocation_context(),
        )
        (prepared.cwd / f"{node_id}.txt").write_text(
            f"{node_id}\n",
            encoding="utf-8",
        )
        prepared.mark_succeeded(defer_cleanup=True)
        prepared.cleanup_after_success()

    try:
        bundle_paths = []
        for node_id in ("implement", "verify", "finalize"):
            state = read_json_object(
                output.create_stage_dir(node_id) / "workspace-state.json"
            )
            assert state["status"] == "succeeded"
            assert state["logical_worktree_name"] == "primary"
            bundle = state["bundle"]
            bundle_path = output.stages_dir / str(bundle["path"])
            assert bundle_path.is_file()
            run_git_text(repo, "bundle", "verify", bundle_path.as_posix())
            bundle_paths.append(bundle_path)
        assert len(set(bundle_paths)) == 3
    finally:
        reuse_cache.cleanup_all_best_effort()
