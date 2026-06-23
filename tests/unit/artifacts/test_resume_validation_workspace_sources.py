from __future__ import annotations

import json

from crewplane.artifacts.resume.validation import validate_resume_frontier
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


def test_validate_frontier_rejects_provider_workspace_wrong_project_source(
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
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    payload["source"]["commit"] = "f" * 40
    payload["invocation_source"]["source_commit"] = "f" * 40
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_provider_workspace_wrong_upstream_source(
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
    a_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
        node_id="a",
    )
    write_lineage_bundle_for_payload(repo, source, a_payload)
    a_result = a_payload["result"]
    assert isinstance(a_result, dict)
    b_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(a_result["result_commit"]),
        source_tree=str(a_result["result_tree"]),
        node_id="b",
        source_kind="node",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(b_payload, a_payload)
    write_lineage_bundle_for_payload(repo, source, b_payload)
    b_payload["source"]["commit"] = "f" * 40
    b_payload["invocation_source"]["source_commit"] = "f" * 40
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(a_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    (source.run_dir / "b" / "workspace-state.json").write_text(
        json.dumps(b_payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_accepts_provider_workspace_upstream_source_bundle(
    tmp_path,
) -> None:
    source, plan, a_payload, b_payload = _source_with_downstream_workspace_state(
        tmp_path
    )
    attach_source_bundle_descriptor(b_payload, a_payload)
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(a_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    (source.run_dir / "b" / "workspace-state.json").write_text(
        json.dumps(b_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "b")

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a", "b")


def test_validate_frontier_rejects_bundle_with_wrong_result_tree(tmp_path) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, payload)
    result = payload["result"]
    assert isinstance(result, dict)
    result["result_tree"] = "f" * 40
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def test_validate_frontier_rejects_provider_workspace_missing_source_bundle(
    tmp_path,
) -> None:
    source, plan, a_payload, b_payload = _source_with_downstream_workspace_state(
        tmp_path
    )
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(a_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    (source.run_dir / "b" / "workspace-state.json").write_text(
        json.dumps(b_payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_provider_workspace_wrong_source_bundle(
    tmp_path,
) -> None:
    source, plan, a_payload, b_payload = _source_with_downstream_workspace_state(
        tmp_path
    )
    attach_source_bundle_descriptor(b_payload, a_payload)
    source_descriptor = b_payload["source"]
    invocation_source = b_payload["invocation_source"]
    assert isinstance(source_descriptor, dict)
    assert isinstance(invocation_source, dict)
    source_descriptor["bundle_sha256"] = "f" * 64
    invocation_source["source_bundle_sha256"] = "f" * 64
    (source.run_dir / "a" / "workspace-state.json").write_text(
        json.dumps(a_payload),
        encoding="utf-8",
    )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    (source.run_dir / "b" / "workspace-state.json").write_text(
        json.dumps(b_payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ("a",)


def test_validate_frontier_rejects_provider_workspace_wrong_candidate_source(
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
    write_stage_output_file(
        source.run_dir / "a" / "review-audit-round-2" / "alpha_round1.md"
    )
    executor_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    write_lineage_bundle_for_payload(repo, source, executor_payload)
    (source.run_dir / "a" / "workspace-state-executor.json").write_text(
        json.dumps(executor_payload),
        encoding="utf-8",
    )
    executor_result = executor_payload["result"]
    assert isinstance(executor_result, dict)
    reviewer_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(executor_result["result_commit"]),
        source_tree=str(executor_result["result_tree"]),
        source_kind="candidate",
        source_node_id="a",
        candidate_sequence=1,
    )
    attach_source_bundle_descriptor(reviewer_payload, executor_payload)
    reviewer_payload["source"]["commit"] = "f" * 40
    reviewer_payload["invocation_source"]["source_commit"] = "f" * 40
    reviewer_payload["role"] = "reviewer"
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
        "lineage_produced": False,
    }
    reviewer_payload.pop("bundle")
    (source.run_dir / "a" / "workspace-state-reviewer.json").write_text(
        json.dumps(reviewer_payload),
        encoding="utf-8",
    )

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == ()


def _source_with_downstream_workspace_state(tmp_path):
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
    plan = plan.model_copy(update={"nodes": nodes})
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
    a_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
        node_id="a",
    )
    write_lineage_bundle_for_payload(repo, source, a_payload)
    a_result = a_payload["result"]
    assert isinstance(a_result, dict)
    b_payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=str(a_result["result_commit"]),
        source_tree=str(a_result["result_tree"]),
        node_id="b",
        source_kind="node",
        source_node_id="a",
        candidate_sequence=1,
    )
    write_lineage_bundle_for_payload(repo, source, b_payload)
    return source, plan, a_payload, b_payload
