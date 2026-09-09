from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from crewplane.artifacts.manager import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    ProviderRecord,
    WorkspaceSelectionRecord,
    WorkspaceSetupCommandRecord,
    WorkspaceSetupRecord,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workspace.policy import WorktreeContract
from crewplane.runtime.execution.review_loop.workspace_state_paths import (
    workspace_artifact_allowed_paths,
)
from crewplane.runtime.workspace.invocation import invocation_slug, workspace_state_path
from crewplane.runtime.workspace.worktree.lineage import export_bundle
from crewplane.runtime.workspace.worktree.protected_refs import ProtectedRefSnapshot
from crewplane.runtime.workspace.worktree.types import (
    WorktreeCaptureRequest,
    WorktreeSourceRef,
)
from tests.helpers.artifacts import node_artifact_request
from tests.helpers.workspace_service import (
    create_git_repo,
    run_git_text,
    workspace_plan,
)


def test_workspace_artifact_allowlist_empty_without_managed_workspace(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = execution_node(workspace_policy=None)

    allowed_paths = workspace_artifact_allowed_paths(
        output,
        node,
        "alpha",
        ProviderRole.EXECUTOR,
        None,
        1,
    )

    assert allowed_paths == set()
    assert output.get_node_dir(node_artifact_request("implement")) is None


def test_workspace_artifact_allowlist_contains_runtime_owned_workspace_paths(
    tmp_path: Path,
) -> None:
    output = OutputManager("workflow", base_dir=tmp_path)
    node = execution_node(
        workspace_policy=WorkspaceSelectionRecord(
            enabled=True,
            logical_worktree_name="primary",
            declaration_kind="worktree",
            materialization="worktree_checkout",
            worktree_contract=WorktreeContract(),
            setup=WorkspaceSetupRecord(
                profile_name="bootstrap",
                commands=[
                    WorkspaceSetupCommandRecord(
                        argv=["true"],
                        command_index=0,
                    )
                ],
            ),
            writable=True,
            lineage_producer=True,
        )
    )

    allowed_paths = workspace_artifact_allowed_paths(
        output,
        node,
        "alpha",
        ProviderRole.EXECUTOR,
        None,
        1,
    )

    stage_dir = output.get_node_dir(node_artifact_request("implement"))
    assert stage_dir is not None
    slug = invocation_slug("implement", "alpha", None, 1)
    assert allowed_paths == {
        stage_dir / "workspace-state.json",
        stage_dir / "workspace-setup" / "setup.json",
        stage_dir / "workspace-setup" / "setup.log",
        stage_dir / "workspace-bundles" / f"{slug}.bundle",
    }


@pytest.mark.parametrize("slug_length", [40, 120, 121, 160])
def test_exported_bundle_agrees_with_executor_allowlist(
    tmp_path: Path, slug_length: int
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", True, kind="worktree")
    source = plan.workspace_source
    assert source is not None
    node = plan.nodes[0]
    task_id = "a" * (slug_length - len(invocation_slug(node.id, "", None, 1)))
    slug = invocation_slug(node.id, task_id, None, 1)
    assert len(slug) == slug_length
    output = OutputManager("workflow", base_dir=tmp_path)
    state_path = workspace_state_path(output, node, slug, None, 1)
    result_ref = "refs/crewplane/tests/result"
    run_git_text(repo, "update-ref", result_ref, source.run_base_commit)
    request = WorktreeCaptureRequest(
        plan=plan,
        source=source,
        source_ref=WorktreeSourceRef(
            source_kind="project",
            source_node_id=None,
            source_commit=source.run_base_commit,
            source_tree=source.source_tree,
        ),
        workspace_path=repo,
        checkout_root=repo,
        git_dir=repo / ".git",
        node_id=node.id,
        task_id=task_id,
        state_path=state_path,
        slug=slug,
        protected_refs=ProtectedRefSnapshot(scopes=(), refs=()),
    )

    bundle_path = export_bundle(request, result_ref)

    expected_stem = slug
    if slug_length > 120:
        expected_stem = (
            f"{slug[:106]}--{hashlib.sha256(slug.encode()).hexdigest()[:12]}"
        )
    assert (
        bundle_path
        == state_path.parent / "workspace-bundles" / f"{expected_stem}.bundle"
    )
    assert bundle_path.is_file()
    assert bundle_path.parent.stat().st_mode & 0o777 == 0o700
    assert list(bundle_path.parent.iterdir()) == [bundle_path]
    assert bundle_path in workspace_artifact_allowed_paths(
        output, node, task_id, ProviderRole.EXECUTOR, None, 1
    )
    assert bundle_path not in workspace_artifact_allowed_paths(
        output, node, task_id, ProviderRole.REVIEWER, None, 1
    )


def test_snapshot_workspace_does_not_allow_lineage_bundles(tmp_path: Path) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(repo, tmp_path / "cache", True, kind="snapshot")
    output = OutputManager("workflow", base_dir=tmp_path)

    allowed_paths = workspace_artifact_allowed_paths(
        output, plan.nodes[0], "alpha", ProviderRole.EXECUTOR, None, 1
    )

    assert {path.name for path in allowed_paths} == {"workspace-state.json"}


def execution_node(
    workspace_policy: WorkspaceSelectionRecord | None,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="implement",
        mode="sequential",
        provider_records=[
            ProviderRecord(
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature="agent-signature",
                invoker_config_signature="invoker-signature",
            )
        ],
        workspace_policy=workspace_policy,
        artifact_contract=ArtifactContract(
            stage_path="implement",
            output_path="implement/output.md",
        ),
    )
