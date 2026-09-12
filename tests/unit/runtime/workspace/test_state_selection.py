from __future__ import annotations

import os
from pathlib import Path

import pytest

from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution.workspace_files import (
    latest_executor_workspace_state,
)
from crewplane.runtime.workspace.state import discard_workspace_lineage
from crewplane.runtime.workspace.state_selection import (
    required_lineage_state_path,
    workspace_state_paths,
)
from crewplane.runtime.workspace.worktree.source_refs import (
    invocation_source_ref,
)
from tests.helpers.workspace_records import workspace_selection_record
from tests.unit.runtime.workspace.state_selection_support import (
    ArtifactStore,
    runtime_dynamic_locator,
    same_selection_node,
    selection_plan_with_locator,
    selection_source_snapshot,
    write_selection_output,
    write_selection_review_status,
    write_selection_state,
)


def test_required_lineage_state_uses_review_loop_canonical_output(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    stale_state = stage_dir / "workspace-state-aaa-alpha-audit1-round1.json"
    write_selection_output(stage_dir / "review-audit-round-1" / "alpha_round2.md")
    write_selection_review_status(stage_dir, "review-audit-round-1/alpha_round2.md")
    write_selection_state(stale_state, "1" * 40, round_num=1, audit_round_num=1)
    write_selection_state(canonical_state, "2" * 40, round_num=2, audit_round_num=1)

    assert required_lineage_state_path(store, same_selection_node()) == canonical_state


def test_downstream_invocation_source_uses_review_loop_canonical_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    stale_state = stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    write_selection_output(stage_dir / "review-audit-round-1" / "alpha_round2.md")
    write_selection_review_status(stage_dir, "review-audit-round-1/alpha_round2.md")
    write_selection_state(stale_state, "1" * 40, round_num=1, audit_round_num=1)
    write_selection_state(canonical_state, "2" * 40, round_num=2, audit_round_num=1)

    source_ref = invocation_source_ref(
        store,
        _downstream_source_plan(tmp_path),
        _downstream_node(),
        workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="node",
            source_node_id="implement",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        selection_source_snapshot(tmp_path),
        role_label=ProviderRole.EXECUTOR,
        round_num=1,
        audit_round_num=None,
    )

    assert source_ref.source_kind == "node"
    assert source_ref.source_commit == "2" * 40


def test_required_lineage_state_keeps_review_loop_canonical_over_later_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    canonical_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    later_state = stage_dir / "workspace-state-implement-alpha-round3.json"
    write_selection_output(stage_dir / "alpha_round2.md")
    write_selection_review_status(stage_dir, "alpha_round2.md")
    write_selection_state(canonical_state, "2" * 40, round_num=2, audit_round_num=None)
    write_selection_state(later_state, "3" * 40, round_num=3, audit_round_num=None)

    assert required_lineage_state_path(store, same_selection_node()) == canonical_state


def test_same_node_executor_source_skips_discarded_invalid_candidate(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    valid_state = stage_dir / "workspace-state-implement-alpha-audit1-round1.json"
    invalid_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    write_selection_state(valid_state, "1" * 40, round_num=1, audit_round_num=1)
    write_selection_state(invalid_state, "2" * 40, round_num=2, audit_round_num=1)
    discard_workspace_lineage(invalid_state, "invalid_candidate.empty")

    source_ref = invocation_source_ref(
        store,
        selection_plan_with_locator(tmp_path, runtime_dynamic_locator()),
        same_selection_node(),
        workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="project",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        selection_source_snapshot(tmp_path),
        role_label=ProviderRole.EXECUTOR,
        round_num=3,
        audit_round_num=1,
    )

    assert source_ref.source_kind == "candidate"
    assert source_ref.source_commit == "1" * 40


@pytest.mark.parametrize(
    ("role", "round_num", "audit", "expected_kind", "expected_commit"),
    [
        (ProviderRole.REVIEWER, 1, 1, "candidate", "1" * 40),
        (ProviderRole.REVIEWER, 2, 1, "project", "0" * 40),
        (ProviderRole.REVIEWER, 1, 2, "candidate", "1" * 40),
        (ProviderRole.EXECUTOR, 2, 1, "candidate", "1" * 40),
        (ProviderRole.EXECUTOR, 3, 1, "candidate", "1" * 40),
    ],
)
def test_invocation_sources_preserve_role_round_and_seeded_audit_fallbacks(
    tmp_path, role, round_num, audit, expected_kind, expected_commit
) -> None:
    store = ArtifactStore(tmp_path)
    write_selection_state(
        tmp_path / "implement" / "workspace-state-alpha-audit1-round1.json",
        "1" * 40,
        round_num=1,
        audit_round_num=1,
    )
    node = same_selection_node()
    assert node.workspace_policy is not None
    source = invocation_source_ref(
        store,
        selection_plan_with_locator(tmp_path, runtime_dynamic_locator()),
        node,
        node.workspace_policy,
        selection_source_snapshot(tmp_path),
        role,
        round_num,
        audit,
    )
    assert source.source_kind == expected_kind
    assert source.source_commit == expected_commit


def test_required_lineage_state_prefers_latest_executor_without_review_status(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    first_state = stage_dir / "workspace-state.json"
    latest_state = stage_dir / "workspace-state-implement-alpha-round2.json"
    write_selection_state(first_state, "1" * 40, round_num=1, audit_round_num=None)
    write_selection_state(latest_state, "2" * 40, round_num=2, audit_round_num=None)

    assert required_lineage_state_path(store, same_selection_node()) == latest_state


def test_required_lineage_state_resolves_seeded_audit_copy_to_previous_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    previous_state = stage_dir / "workspace-state-implement-alpha-audit1-round2.json"
    write_selection_output(stage_dir / "review-audit-round-2" / "alpha_round1.md")
    write_selection_review_status(stage_dir, "review-audit-round-2/alpha_round1.md")
    write_selection_state(previous_state, "2" * 40, round_num=2, audit_round_num=1)

    assert required_lineage_state_path(store, same_selection_node()) == previous_state


def test_required_lineage_state_fails_when_canonical_status_has_no_state(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "implement"
    write_selection_output(stage_dir / "alpha_round2.md")
    write_selection_review_status(stage_dir, "alpha_round2.md")

    with pytest.raises(RuntimeError, match="no matching succeeded workspace state"):
        required_lineage_state_path(store, same_selection_node())


def test_latest_executor_workspace_state_uses_payload_order_not_filename_order(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    stage_dir = tmp_path / "build"
    write_selection_state(
        stage_dir / "workspace-state-z-alpha-round9.json",
        "9" * 40,
        round_num=9,
        audit_round_num=None,
    )
    write_selection_state(
        stage_dir / "workspace-state-a-alpha-round10.json",
        "a" * 40,
        round_num=10,
        audit_round_num=None,
    )

    build_node = same_selection_node().model_copy(
        update={
            "id": "build",
            "artifact_contract": ArtifactContract(
                stage_path="build",
                output_path="build.md",
                log_path="build/logs",
                result_path="build.md",
            ),
        }
    )
    state = latest_executor_workspace_state(store, build_node)

    assert state["result"]["result_commit"] == "a" * 40


def test_workspace_state_paths_rejects_symlink_state(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (stage_dir / "workspace-state.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Unsafe workspace state artifact"):
        workspace_state_paths(stage_dir)


def test_workspace_state_paths_rejects_hardlinked_state(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside = tmp_path / "outside-state.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        os.link(outside, stage_dir / "workspace-state.json")
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(RuntimeError, match="Unsafe workspace state artifact"):
        workspace_state_paths(stage_dir)


def _downstream_node() -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="verify",
        mode="sequential",
        dependencies=["implement"],
        render_plan_id="verify",
        provider_records=[
            ProviderRecord(
                provider="alpha",
                role=ProviderRole.EXECUTOR,
                task_id="alpha",
                agent_config_key="alpha",
                invoker_alias="mock",
                agent_config_signature="agent",
                invoker_config_signature="invoker",
            )
        ],
        workspace_policy=workspace_selection_record(
            enabled=True,
            kind="worktree",
            source_kind="node",
            source_node_id="implement",
            clean_start="strict",
            materialization="worktree_checkout",
        ),
        artifact_contract=ArtifactContract(
            stage_path="verify",
            output_path="verify.md",
            log_path="verify/logs",
            result_path="verify.md",
        ),
    )


def _downstream_source_plan(tmp_path: Path) -> PreflightExecutionPlan:
    plan = selection_plan_with_locator(tmp_path, runtime_dynamic_locator())
    downstream = _downstream_node()
    return plan.model_copy(
        update={
            "execution_order": ["implement", "verify"],
            "nodes": [same_selection_node(), downstream],
            "render_plans": [
                RenderPlan(render_plan_id="implement", node_id="implement"),
                RenderPlan(render_plan_id="verify", node_id="verify"),
            ],
            "workspace_file_locators": [],
        }
    )
