from __future__ import annotations

import json

from orchestrator_cli.artifacts.resume.validation import validate_resume_frontier
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    attach_source_bundle_descriptor,
    provider_workspace_state_payload,
    source_record,
    write_lineage_bundle_for_payload,
    write_review_status_file,
    write_stage_output_file,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_validate_frontier_accepts_seeded_audit_candidate_source(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    write_review_status_file(
        source.run_dir / "a", "review-audit-round-2/alpha_round1.md"
    )
    status_path = source.run_dir / "a" / "review-state" / "review-loop-status.json"
    status_payload = json.loads(status_path.read_text(encoding="utf-8"))
    status_payload["reviewer_outputs"] = [
        {
            "task_id": "beta",
            "provider": "beta",
            "role": "reviewer",
            "path": "review-audit-round-2/beta_round1.md",
        }
    ]
    status_path.write_text(json.dumps(status_payload), encoding="utf-8")
    write_stage_output_file(
        source.run_dir / "a" / "review-audit-round-2" / "alpha_round1.md"
    )
    write_stage_output_file(
        source.run_dir / "a" / "review-audit-round-2" / "beta_round1.md"
    )
    first_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    first_payload["audit_round_num"] = 1
    write_lineage_bundle_for_payload(repo, source, first_payload)
    first_result = first_payload["result"]
    assert isinstance(first_result, dict)
    second_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(first_result["result_commit"]),
        source_tree=str(first_result["result_tree"]),
        source_kind="candidate",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(second_payload, first_payload)
    second_payload["round_num"] = 2
    second_payload["audit_round_num"] = 1
    write_lineage_bundle_for_payload(repo, source, second_payload)
    second_result = second_payload["result"]
    assert isinstance(second_result, dict)
    reviewer_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(second_result["result_commit"]),
        source_tree=str(second_result["result_tree"]),
        source_kind="candidate",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(reviewer_payload, second_payload)
    reviewer_payload["task_id"] = "beta"
    reviewer_payload["provider"] = "beta"
    reviewer_payload["role"] = "reviewer"
    reviewer_payload["round_num"] = 1
    reviewer_payload["audit_round_num"] = 2
    reviewer_payload["workspace"] = {
        "path": None,
        "effective_cwd": None,
        "materialization": "worktree_checkout",
        "writable": True,
        "lineage_producer": False,
        "retention": "retained",
        "retained_reason": None,
        "project_root_relative_path": ".",
    }
    reviewer_payload["result"] = {
        "changed_path_count": 0,
        "final_head": second_result["result_commit"],
        "lineage_produced": False,
    }
    reviewer_payload.pop("refs")
    reviewer_payload.pop("bundle")
    for path, payload in (
        (
            source.run_dir / "a" / "workspace-state-alpha-audit1-round1.json",
            first_payload,
        ),
        (
            source.run_dir / "a" / "workspace-state-alpha-audit1-round2.json",
            second_payload,
        ),
        (
            source.run_dir / "a" / "workspace-state-beta-audit2-round1.json",
            reviewer_payload,
        ),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_accepts_downstream_review_loop_lineage(
    tmp_path,
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    source_policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    downstream_policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        source_kind="node",
        source_node_id="a",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    nodes = [
        plan.nodes[0].model_copy(update={"workspace_policy": source_policy}),
        plan.nodes[1].model_copy(update={"workspace_policy": downstream_policy}),
    ]
    plan = plan.model_copy(
        update={
            "nodes": nodes,
        }
    )
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    a_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    b_descriptor = write_result(source.results_dir, "b-result.md", "b output")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [a_descriptor])
    )
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "b", [b_descriptor])
    )
    write_review_status_file(
        source.run_dir / "a", "review-audit-round-1/alpha_round2.md"
    )
    write_stage_output_file(
        source.run_dir / "a" / "review-audit-round-1" / "alpha_round2.md"
    )
    first_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    first_payload["audit_round_num"] = 1
    write_lineage_bundle_for_payload(repo, source, first_payload)
    first_result = first_payload["result"]
    assert isinstance(first_result, dict)
    second_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(first_result["result_commit"]),
        source_tree=str(first_result["result_tree"]),
        source_kind="candidate",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(second_payload, first_payload)
    second_payload["round_num"] = 2
    second_payload["audit_round_num"] = 1
    write_lineage_bundle_for_payload(repo, source, second_payload)
    second_result = second_payload["result"]
    assert isinstance(second_result, dict)
    downstream_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(second_result["result_commit"]),
        source_tree=str(second_result["result_tree"]),
        node_id="b",
        source_kind="node",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(downstream_payload, second_payload)
    write_lineage_bundle_for_payload(repo, source, downstream_payload)
    for path, payload in (
        (
            source.run_dir / "a" / "workspace-state-alpha-audit1-round1.json",
            first_payload,
        ),
        (
            source.run_dir / "a" / "workspace-state-alpha-audit1-round2.json",
            second_payload,
        ),
        (source.run_dir / "b" / "workspace-state.json", downstream_payload),
    ):
        path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    attach_workspace_descriptor(source.run_dir, plan, "b")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a", "b")
