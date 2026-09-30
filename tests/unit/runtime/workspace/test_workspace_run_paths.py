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
from crewplane.runtime.workspace.snapshot import create_snapshot_workspace
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


@pytest.mark.parametrize("entry_kind", ["file", "symlink"])
def test_reviewer_allocation_rejects_unsafe_parent(
    tmp_path: Path, entry_kind: str
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo, tmp_path / "cache", cleanup_on_success=True, kind="worktree"
    )
    source = plan.workspace_source
    assert source is not None
    planned = planned_workspace_path(
        plan, source, "review-workspaces", "invocation", "import/build"
    )
    planned.parent.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if entry_kind == "file":
        planned.parent.write_text("keep")
    else:
        planned.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError) as caught:
        allocate_worktree_workspace(
            plan, "invocation", source, "review-workspaces", "import/build"
        )

    assert str(caught.value).endswith(planned.parent.as_posix())
    assert not planned.exists()
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["snapshot", "worktree"])
@pytest.mark.parametrize("entry_kind", ["file", "directory", "dangling_symlink"])
def test_allocation_preserves_existing_invocation_paths(
    tmp_path: Path, kind: str, entry_kind: str
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", cleanup_on_success=True, kind=kind)
    source = plan.workspace_source
    assert source is not None
    family = "snapshots" if kind == "snapshot" else "workspaces"
    planned = planned_workspace_path(plan, source, family, "invocation")
    planned.parent.mkdir(parents=True)
    if entry_kind == "file":
        planned.write_text("keep")
    elif entry_kind == "directory":
        planned.mkdir()
        (planned / "keep").write_text("keep")
    else:
        planned.symlink_to(tmp_path / "missing")

    with pytest.raises(RuntimeError, match="Workspace path already exists"):
        if kind == "snapshot":
            create_snapshot_workspace(plan, "invocation", source)
        else:
            allocate_worktree_workspace(plan, "invocation", source, "workspaces", None)

    if entry_kind == "file":
        assert planned.read_text() == "keep"
    elif entry_kind == "directory":
        assert (planned / "keep").read_text() == "keep"
    else:
        assert planned.is_symlink()
