from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.invocation_identity import invocation_slug
from crewplane.runtime.workspace import prepare_invocation_workspace
from crewplane.runtime.workspace.filesystem import (
    workspace_run_root,
)
from crewplane.runtime.workspace.service.common import planned_workspace_path
from crewplane.runtime.workspace.worktree.checkout_placement import (
    allocate_worktree_workspace,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    read_json_object,
    workspace_invocation_context,
    workspace_invocation_request,
    workspace_output_manager,
    workspace_plan,
)


@pytest.mark.parametrize(
    ("kind", "role", "family", "parent"),
    [
        ("snapshot", ProviderRole.EXECUTOR, "snapshots", None),
        ("worktree", ProviderRole.EXECUTOR, "workspaces", None),
        ("worktree", ProviderRole.REVIEWER, "review-workspaces", "implement"),
    ],
)
def test_planned_allocated_and_persisted_workspace_paths_agree(
    tmp_path: Path, kind, role, family, parent
) -> None:
    repo = create_git_repo(tmp_path)
    cache = tmp_path / "cache"
    plan = workspace_plan(repo, cache, cleanup_on_success=True, kind=kind)
    source = plan.workspace_source
    assert source is not None
    output = workspace_output_manager(tmp_path, repo)
    stage = output.create_node_dir(node_artifact_request("implement"))
    request = workspace_invocation_request(plan, output, role_label=role)
    slug = invocation_slug(
        request.node_id, request.task_id, request.audit_round_num, request.round_num
    )
    expected = cache / family / source.repository_id / plan.run_key_name
    if parent is not None:
        expected /= parent
    expected /= slug

    assert planned_workspace_path(plan, source, family, slug, parent) == expected
    assert not cache.exists()
    prepared = prepare_invocation_workspace(
        request, workspace_invocation_context(role=role)
    )
    assert prepared.workspace_path == expected
    state = read_json_object(stage / "workspace-state.json")
    assert state["execution"]["workspace_path"] == expected.as_posix()
    assert expected.stat().st_mode & 0o777 == 0o700
    prepared.mark_succeeded()


@pytest.mark.parametrize("ancestor_index", range(4))
@pytest.mark.parametrize("entry_kind", ["file", "symlink"])
def test_allocation_rejects_each_unsafe_ancestor_in_order(
    tmp_path: Path, ancestor_index: int, entry_kind: str
) -> None:
    repo = create_git_repo(tmp_path)
    cache = tmp_path / "cache"
    plan = workspace_plan(repo, cache, cleanup_on_success=True, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    family = cache / "workspaces"
    repository = family / source.repository_id
    run = repository / plan.run_key_name
    hierarchy = (cache, family, repository, run)
    blocked = hierarchy[ancestor_index]
    blocked.parent.mkdir(parents=True, exist_ok=True)
    if entry_kind == "file":
        blocked.write_text("keep")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        blocked.symlink_to(outside, target_is_directory=True)

    assert planned_workspace_path(plan, source, "workspaces", "invocation") == (
        run / "invocation"
    )
    with pytest.raises(RuntimeError) as exc_info:
        workspace_run_root(plan, source, "workspaces")
    assert str(exc_info.value).endswith(blocked.as_posix())
    for ancestor in hierarchy[:ancestor_index]:
        assert ancestor.stat().st_mode & 0o777 == 0o700
    if entry_kind == "symlink":
        assert list(outside.iterdir()) == []


def test_reviewer_parent_sanitization_matches_allocation(tmp_path: Path) -> None:
    repo = create_git_repo(tmp_path)
    cache = tmp_path / "cache"
    plan = workspace_plan(repo, cache, cleanup_on_success=True, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    planned = planned_workspace_path(
        plan, source, "review-workspaces", "invocation", "import/build"
    )
    assert not cache.exists()
    allocated, checkout = allocate_worktree_workspace(
        plan, "invocation", source, "review-workspaces", "import/build"
    )
    assert allocated == planned
    assert checkout == planned / "checkout"
