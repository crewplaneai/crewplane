from __future__ import annotations

import json

import pytest

import crewplane.artifacts.resume.hydration as hydration_module
from crewplane.artifacts.naming import (
    build_node_state_filename,
)
from crewplane.artifacts.resume.validation import (
    ValidatedResumeFrontier,
    validate_resume_frontier,
)
from crewplane.artifacts.run_history import find_same_context_runs
from crewplane.core.execution_state import NodeState
from crewplane.runtime.workspace.state_selection import (
    required_lineage_state_path,
)
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    attach_workspace_descriptor,
    make_node_state,
    make_provider_record,
    make_run_manifest,
    sha256_hex,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.helpers.resume_validation import snapshot_workspace_state_payload
from tests.unit.artifacts.resume_hydration_support import (
    hydration_output,
    workspace_snapshot_plan,
    workspace_worktree_plan,
    write_lineage_workspace_state,
)


def test_hydrate_resume_frontier_preserves_review_loop_canonical_lineage(
    tmp_path,
) -> None:
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    write_run_manifest(tmp_path, manifest)
    source = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    plan = workspace_worktree_plan()
    stage_dir = source.run_dir / "a"
    write_lineage_workspace_state(
        stage_dir / "workspace-state-a-alpha-round2.json",
        "2" * 40,
        2,
    )
    write_lineage_workspace_state(
        stage_dir / "workspace-state-a-alpha-round3.json",
        "3" * 40,
        3,
    )
    (stage_dir / "alpha_round2.md").write_text("canonical\n", encoding="utf-8")
    _write_review_status(stage_dir, "alpha_round2.md")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    node_state_path = (
        source.run_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    frontier = ValidatedResumeFrontier(source, {"a": node_state})
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    assert (
        required_lineage_state_path(output, plan.nodes[0]).name
        == "workspace-state-a-alpha-round2.json"
    )
    assert (
        output.stages_dir / "a" / "review-state" / "review-loop-status.json"
    ).is_file()
    assert (output.stages_dir / "a" / "alpha_round2.md").is_file()


@pytest.mark.parametrize(
    "relative_path",
    ["alpha_round2.md", "review-state/review-loop-status.json"],
)
def test_hydrate_resume_frontier_rechecks_review_loop_artifact_hash(
    tmp_path,
    relative_path: str,
) -> None:
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    write_run_manifest(tmp_path, manifest)
    source = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    plan = workspace_worktree_plan()
    stage_dir = source.run_dir / "a"
    write_lineage_workspace_state(
        stage_dir / "workspace-state-a-alpha-round2.json",
        "2" * 40,
        2,
    )
    (stage_dir / "alpha_round2.md").write_text("canonical\n", encoding="utf-8")
    _write_review_status(stage_dir, "alpha_round2.md")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    node_state_path = (
        source.run_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    (stage_dir / relative_path).write_text("tampered\n", encoding="utf-8")
    frontier = ValidatedResumeFrontier(source, {"a": node_state})
    output = hydration_output(tmp_path)

    with pytest.raises(ValueError, match="Workspace resume artifact hash changed"):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)


def test_hydrated_parallel_workspace_state_can_be_resumed_without_raw_outputs(
    tmp_path,
) -> None:
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    write_run_manifest(tmp_path, manifest)
    source = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [descriptor]),
    )
    plan = _parallel_workspace_snapshot_plan()
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(parents=True, exist_ok=True)
    for task_id in ("alpha", "beta"):
        payload = snapshot_workspace_state_payload(source, plan, task_id)
        (stage_dir / f"workspace-state-{task_id}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    attach_workspace_descriptor(source.run_dir, plan, "a")
    frontier = validate_resume_frontier(source, plan)
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)
    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="failed")
    )
    hydrated_source = next(
        record
        for record in find_same_context_runs(
            tmp_path,
            WORKFLOW_IDENTITY,
            WORKFLOW_NAME,
            WORKFLOW_SIGNATURE,
        )
        if record.manifest.run_key_name == output.run_key_name
    )
    hydrated_frontier = validate_resume_frontier(hydrated_source, plan)

    assert frontier.resumed_node_ids == ("a",)
    assert hydrated_frontier.resumed_node_ids == ("a",)
    assert not (output.stages_dir / "a" / "alpha_round1.md").exists()
    assert not (output.stages_dir / "a" / "beta_round1.md").exists()


def _parallel_workspace_snapshot_plan():
    plan = workspace_snapshot_plan()
    node = plan.nodes[0].model_copy(
        update={
            "mode": "parallel",
            "provider_records": [
                make_provider_record("alpha"),
                make_provider_record("beta"),
            ],
        }
    )
    return plan.model_copy(update={"nodes": [node, plan.nodes[1]]})


def _write_review_status(stage_dir, canonical_path: str) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True, exist_ok=True)
    canonical_bytes = (stage_dir / canonical_path).read_bytes()
    (status_dir / "review-loop-status.json").write_text(
        json.dumps(
            {
                "node_id": "a",
                "executed_audit_rounds": 1,
                "attempted_local_round_num": 2,
                "final_local_round_num": 2,
                "invalid_candidate_round_count": 0,
                "no_progress_round_count": 0,
                "artifact_drift_warning_count": 0,
                "consensus_reached": True,
                "continued_after_consensus_exhaustion": False,
                "canonical_executor_outputs": [
                    {
                        "task_id": "alpha",
                        "provider": "alpha",
                        "role": "executor",
                        "path": canonical_path,
                        "sha256": sha256_hex(canonical_bytes),
                        "size_bytes": len(canonical_bytes),
                        "audit_round_num": None,
                        "round_num": 2,
                    }
                ],
                "reviewer_outputs": [],
            }
        ),
        encoding="utf-8",
    )
