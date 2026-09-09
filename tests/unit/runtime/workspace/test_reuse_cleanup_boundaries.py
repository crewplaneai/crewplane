from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.service import MaterializationLimiter
from crewplane.runtime.workspace.worktree import cache as cache_module
from crewplane.runtime.workspace.worktree.cache import (
    ReusableWorktreeCheckout,
    WorktreeReuseCache,
)
from crewplane.runtime.workspace.worktree.types import WorktreeSourceRef
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_output_manager,
)
from tests.helpers.workspace_worktree_reuse import (
    two_node_lineage_plan,
    workspace_request,
)

isolated_git = isolated_git_support.isolated_git


@dataclass(frozen=True)
class CachedCheckout:
    cache: WorktreeReuseCache
    entry: ReusableWorktreeCheckout


@pytest.fixture
def cached_checkout(
    tmp_path: Path, isolated_git: IsolatedGit
) -> Iterator[CachedCheckout]:
    del isolated_git
    repo = create_git_repo(tmp_path)
    plan = two_node_lineage_plan(repo, tmp_path / "cache")
    output = workspace_output_manager(tmp_path, repo)
    output.create_node_dir(node_artifact_request("implement"))
    cache = WorktreeReuseCache()
    prepared = prepare_invocation_workspace(
        workspace_request(
            plan, output, "implement", cache, MaterializationLimiter.from_plan(plan)
        ),
        workspace_invocation_context(),
    )
    prepared.mark_succeeded(defer_cleanup=True)
    assert prepared.state_path is not None
    assert prepared.reuse_key is not None
    assert plan.workspace_source is not None
    payload = read_json_object(prepared.state_path)
    result = payload["result"]
    assert isinstance(result, dict)
    source_ref = WorktreeSourceRef(
        "node", "implement", str(result["result_commit"]), str(result["result_tree"])
    )
    entry = cache.take(
        prepared.reuse_key,
        source_ref,
        plan.workspace_source.repository_id,
        plan.run_key_name,
    )
    assert entry is not None
    cache.store(entry)
    try:
        yield CachedCheckout(cache, entry)
    finally:
        cache.cleanup_all_best_effort()


def test_reuse_cache_does_not_own_missing_or_discarded_entries(
    cached_checkout: CachedCheckout, tmp_path: Path
) -> None:
    cache, entry = cached_checkout.cache, cached_checkout.entry
    assert not cache.owns(None)
    assert cache.cleanup_workspace(tmp_path / "unowned") == ()
    assert cache.cleanup_workspace_best_effort(tmp_path / "unowned") is None
    cache.discard_workspace(entry.workspace_path)
    assert not cache.owns(entry.workspace_path)
    assert entry.workspace_path.exists()
    cache.store(entry)


def test_reuse_cleanup_reports_externally_deleted_checkout(
    cached_checkout: CachedCheckout,
) -> None:
    cache, entry = cached_checkout.cache, cached_checkout.entry
    shutil.rmtree(entry.workspace_path)
    error = cache.cleanup_workspace_best_effort(entry.workspace_path)
    assert isinstance(error, RuntimeError)
    assert "absent without completed cleanup evidence" in str(error)
    assert cache.owns(entry.workspace_path)
    errors = cache.cleanup_node_best_effort(entry.node_id)
    assert len(errors) == 1
    assert "absent without completed cleanup evidence" in str(errors[0])


def test_reuse_cleanup_preserves_reappeared_path_after_state_write_failure(
    cached_checkout: CachedCheckout,
) -> None:
    cache, entry = cached_checkout.cache, cached_checkout.entry
    with (
        patch.object(
            cache_module,
            "update_workspace_retention",
            side_effect=OSError("state write failed"),
        ),
        pytest.raises(OSError, match="state write failed"),
    ):
        cache.cleanup_workspace(entry.workspace_path)
    assert not entry.workspace_path.exists()
    assert cache.owns(entry.workspace_path)
    entry.workspace_path.mkdir()
    sentinel = entry.workspace_path / "new-owner"
    sentinel.write_bytes(b"keep")
    with pytest.raises(RuntimeError, match="reappeared after physical cleanup"):
        cache.cleanup_workspace(entry.workspace_path)
    assert sentinel.read_bytes() == b"keep"
    shutil.rmtree(entry.workspace_path)
    assert cache.cleanup_workspace(entry.workspace_path) == (entry.state_path,)
    state = read_json_object(entry.state_path)
    workspace = state["workspace"]
    assert isinstance(workspace, dict)
    assert workspace["retention"] == "deleted"
    assert not cache.owns(entry.workspace_path)


def test_reuse_cleanup_accepts_cancellation_probe(
    cached_checkout: CachedCheckout,
) -> None:
    cache, entry = cached_checkout.cache, cached_checkout.entry
    probes = 0

    def cancel_requested() -> bool:
        nonlocal probes
        probes += 1
        return False

    assert cache.cleanup_workspace(entry.workspace_path, cancel_requested) == (
        entry.state_path,
    )
    assert probes > 0
    assert not entry.workspace_path.exists()


@pytest.mark.parametrize("field", ["repository_id", "run_key_name"])
def test_cache_rejects_entry_without_run_ownership(
    cached_checkout: CachedCheckout, field: str
) -> None:
    entry = (
        replace(cached_checkout.entry, repository_id="")
        if field == "repository_id"
        else replace(cached_checkout.entry, run_key_name="")
    )
    cache = WorktreeReuseCache()
    with pytest.raises(RuntimeError, match="lacks repository and run ownership"):
        cache.store(entry)
    assert not cache.owns(entry.workspace_path)
