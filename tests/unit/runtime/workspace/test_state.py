from __future__ import annotations

import json
from pathlib import Path

from orchestrator_cli.runtime.workspace.state import (
    WorkspaceStateMaterializationRequest,
    WorkspaceStateRetention,
    WorkspaceStateUpdateRequest,
    WorkspaceStateWriteRequest,
    discard_workspace_lineage,
    update_workspace_state,
    write_running_workspace_state,
)
from orchestrator_cli.runtime.workspace.worktree_types import WorktreeSourceRef
from tests.helpers.workspace_service import create_git_repo, workspace_plan


def test_running_workspace_state_records_node_source_bundle_descriptor(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    policy = policy.model_copy(
        update={"source_kind": "node", "source_node_id": "implement"}
    )
    state_path = tmp_path / "run" / "verify" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    bundle_path = tmp_path / "run" / "implement" / "workspace-bundles" / "result.bundle"
    request = WorkspaceStateWriteRequest(
        run_id=plan.run_id,
        run_key_name=plan.run_key_name,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        task_id="alpha",
        provider="alpha",
        role_label="executor",
        round_num=1,
        audit_round_num=None,
        invoker={"launch_mode": "mock_no_child_process"},
    )

    write_running_workspace_state(
        state_path,
        request,
        node,
        source,
        policy,
        WorkspaceStateMaterializationRequest(
            workspace_path=tmp_path / "workspace",
            child_environment_required=False,
            source_ref=WorktreeSourceRef(
                source_kind="node",
                source_node_id="implement",
                source_commit="1" * 40,
                source_tree="2" * 40,
                candidate_sequence=1,
                bundle_path=bundle_path,
                bundle_sha256="3" * 64,
                bundle_size_bytes=123,
                bundle_ref="refs/orchestrator-cli/runs/run/implement/result",
            ),
            materialization="worktree_checkout",
            lineage_producer=True,
        ),
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["source"] == {
        "kind": "node",
        "node_id": "implement",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "candidate_sequence": 1,
        "bundle_path": "implement/workspace-bundles/result.bundle",
        "bundle_sha256": "3" * 64,
        "bundle_size_bytes": 123,
        "bundle_ref": "refs/orchestrator-cli/runs/run/implement/result",
    }
    assert (
        payload["invocation_source"]["source_bundle_path"]
        == "implement/workspace-bundles/result.bundle"
    )
    assert payload["invocation_source"]["source_bundle_sha256"] == "3" * 64
    assert payload["invocation_source"]["source_bundle_size_bytes"] == 123
    assert (
        payload["invocation_source"]["source_bundle_ref"]
        == "refs/orchestrator-cli/runs/run/implement/result"
    )


def test_running_workspace_state_records_source_chain(
    tmp_path: Path,
) -> None:
    repo = create_git_repo(tmp_path)
    plan = workspace_plan(
        repo,
        tmp_path / "cache",
        cleanup_on_success=True,
        kind="worktree",
    )
    source = plan.workspace_source
    assert source is not None
    node = plan.nodes[0]
    policy = node.workspace_policy
    assert policy is not None
    state_path = tmp_path / "run" / "verify" / "workspace-state.json"
    state_path.parent.mkdir(parents=True)
    request = WorkspaceStateWriteRequest(
        run_id=plan.run_id,
        run_key_name=plan.run_key_name,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        task_id="alpha",
        provider="alpha",
        role_label="executor",
        round_num=1,
        audit_round_num=None,
        invoker={"launch_mode": "mock_no_child_process"},
    )

    write_running_workspace_state(
        state_path,
        request,
        node,
        source,
        policy,
        WorkspaceStateMaterializationRequest(
            workspace_path=tmp_path / "workspace",
            child_environment_required=False,
            source_ref=WorktreeSourceRef(
                source_kind="node",
                source_node_id="verify",
                source_commit="4" * 40,
                source_tree="5" * 40,
                candidate_sequence=1,
                bundle_path=tmp_path
                / "run"
                / "verify"
                / "workspace-bundles"
                / "b.bundle",
                bundle_sha256="6" * 64,
                bundle_size_bytes=456,
                bundle_ref="refs/orchestrator-cli/runs/run/verify/result",
                upstream_sources=(
                    WorktreeSourceRef(
                        source_kind="node",
                        source_node_id="implement",
                        source_commit="1" * 40,
                        source_tree="2" * 40,
                        candidate_sequence=1,
                        bundle_path=tmp_path
                        / "run"
                        / "implement"
                        / "workspace-bundles"
                        / "a.bundle",
                        bundle_sha256="3" * 64,
                        bundle_size_bytes=123,
                        bundle_ref="refs/orchestrator-cli/runs/run/implement/result",
                    ),
                ),
            ),
            materialization="worktree_checkout",
            lineage_producer=True,
        ),
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    upstreams = payload["source"]["upstream_sources"]

    assert len(upstreams) == 1
    assert upstreams[0]["node_id"] == "implement"
    assert upstreams[0]["bundle_path"] == "implement/workspace-bundles/a.bundle"


def test_cleanup_update_preserves_terminal_child_environment_false(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {
                    "retention": "pending_cleanup",
                    "retained_reason": "stage_finalization_pending",
                },
                "child_process_environment": {
                    "required": True,
                    "applied": False,
                },
            }
        ),
        encoding="utf-8",
    )

    update_workspace_state(
        state_path,
        WorkspaceStateUpdateRequest(
            status="succeeded",
            retention=WorkspaceStateRetention(
                retention="deleted",
                retained_reason=None,
            ),
        ),
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["child_process_environment"]["applied"] is False
    assert payload["workspace"]["retention"] == "deleted"


def test_discard_workspace_lineage_removes_lineage_result_fields(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "workspace-state.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "workspace": {"lineage_producer": True},
                "result": {
                    "candidate_commit": "a" * 40,
                    "result_commit": "b" * 40,
                    "candidate_tree": "c" * 40,
                    "result_tree": "d" * 40,
                    "changed_path_count": 2,
                    "unreachable_provider_objects_scanned": False,
                },
                "refs": {"result": "refs/result"},
                "bundle": {"path": "workspace-bundles/result.bundle"},
                "diagnostics": [],
            }
        ),
        encoding="utf-8",
    )

    discard_workspace_lineage(state_path, "invalid_candidate.empty")

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["workspace"]["lineage_producer"] is False
    assert "refs" not in payload
    assert "bundle" not in payload
    result = payload["result"]
    assert result["lineage_produced"] is False
    assert result["lineage_discarded"] is True
    assert result["lineage_discard_reason"] == "invalid_candidate.empty"
    assert result["changed_path_count"] == 2
    assert result["final_head"] == "b" * 40
    assert "candidate_commit" not in result
    assert "result_commit" not in result
    assert "candidate_tree" not in result
    assert "result_tree" not in result
    assert payload["diagnostics"][-1]["message"].endswith("invalid_candidate.empty")
