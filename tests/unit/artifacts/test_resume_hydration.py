from __future__ import annotations

import json

import pytest

from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.artifacts.naming import (
    build_generated_file_result_dir_name,
    build_node_state_filename,
)
from orchestrator_cli.artifacts.resume.hydration import hydrate_resume_frontier
from orchestrator_cli.artifacts.resume.validation import (
    ValidatedResumeFrontier,
    validate_resume_frontier,
)
from orchestrator_cli.artifacts.run_history import find_same_context_runs
from orchestrator_cli.core.execution_state import ArtifactDescriptor, NodeState
from orchestrator_cli.runtime.workspace.state_selection import (
    required_lineage_state_path,
)
from orchestrator_cli.version import SCHEMA_VERSION
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    make_provider_record,
    make_run_manifest,
    make_workspace_source_snapshot,
    sha256_hex,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.helpers.resume_validation import snapshot_workspace_state_payload
from tests.helpers.workspace_records import workspace_selection_record


def validated_frontier(
    tmp_path,
    include_findings: bool = True,
    findings_edge: bool = False,
):
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    write_run_manifest(tmp_path, manifest)
    source = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    descriptors = [output_descriptor]
    if include_findings:
        descriptors.append(
            write_result(source.results_dir, "a-findings.md", "findings")
        )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", descriptors),
    )
    plan = make_plan(findings_edge=findings_edge)
    return validate_resume_frontier(source, plan), plan


def test_hydrate_resume_frontier_copies_only_required_artifacts(tmp_path) -> None:
    frontier, plan = validated_frontier(tmp_path)
    output = OutputManager("Workflow", base_dir=tmp_path)

    resumed = hydrate_resume_frontier(frontier, plan, output)

    assert resumed == ("a",)
    assert (output.results_dir / "a-result.md").read_text(
        encoding="utf-8"
    ) == "a output"
    assert not (output.results_dir / "a-findings.md").exists()
    resume_source = json.loads(
        (output.stages_dir / "a" / "resume-source.json").read_text(encoding="utf-8")
    )
    assert resume_source["source_run_id"] == "source"
    assert resume_source["source_node_id"] == "a"
    assert resume_source["result_sha256"]
    assert "restored_at" in resume_source
    assert "findings_sha256" not in resume_source
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["resume_origin"]["source_run_id"] == "source"
    assert node_state["resume_origin"]["hydrated_at"] == resume_source["restored_at"]
    assert node_state["run_id"] == output.run_id


def test_hydrate_rechecks_source_hash_after_validation(tmp_path) -> None:
    frontier, plan = validated_frontier(tmp_path, include_findings=False)
    source_path = frontier.source.results_dir / "a-result.md"
    source_path.write_text("mutated", encoding="utf-8")
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="hash changed"):
        hydrate_resume_frontier(frontier, plan, output)


def test_hydrate_resume_frontier_records_findings_hash_when_required(tmp_path) -> None:
    frontier, plan = validated_frontier(
        tmp_path,
        include_findings=True,
        findings_edge=True,
    )
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    resume_source = json.loads(
        (output.stages_dir / "a" / "resume-source.json").read_text(encoding="utf-8")
    )
    assert resume_source["findings_sha256"]
    assert (output.results_dir / "a-findings.md").read_text(
        encoding="utf-8"
    ) == "findings"


def test_hydrate_resume_frontier_copies_generated_file_sidecars(tmp_path) -> None:
    manifest = make_run_manifest("source", "workflow--source", status="failed")
    write_run_manifest(tmp_path, manifest)
    source = find_same_context_runs(
        tmp_path,
        WORKFLOW_IDENTITY,
        WORKFLOW_NAME,
        WORKFLOW_SIGNATURE,
    )[0]
    output_descriptor = write_result(
        source.results_dir,
        "a-result.md",
        "Updated [alpha/src/app.txt](generated-files/a/alpha/src/app.txt)\n",
    )
    generated_descriptor = write_generated_file(
        source.results_dir,
        "generated-files/a/alpha/src/app.txt",
        "generated content",
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, "a", [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )
    plan = make_plan()
    frontier = validate_resume_frontier(source, plan)
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    hydrated_file = output.results_dir / "generated-files/a/alpha/src/app.txt"
    assert hydrated_file.read_text(encoding="utf-8") == "generated content"
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["generated_files"][0]["relative_path"] == (
        "generated-files/a/alpha/src/app.txt"
    )


def test_hydrate_resume_frontier_copies_generated_file_from_bounded_node_directory(
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
    node_id = "build." + ("x" * 150)
    node_dir = build_generated_file_result_dir_name(node_id)
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
    relative_path = f"generated-files/{node_dir}/alpha/src/app.txt"
    generated_descriptor = write_generated_file(
        source.results_dir,
        relative_path,
        "generated content",
    )
    write_node_state(
        source.run_dir,
        make_node_state(source.manifest, node_id, [output_descriptor]).model_copy(
            update={"generated_files": [generated_descriptor]}
        ),
    )
    plan = _single_node_plan(node_id)
    frontier = validate_resume_frontier(source, plan)
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    hydrated_file = output.results_dir / relative_path
    assert hydrated_file.read_text(encoding="utf-8") == "generated content"
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["generated_files"][0]["relative_path"] == relative_path


def test_hydrate_resume_frontier_rewrites_workspace_state_run_identity(
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
    plan = _workspace_snapshot_plan()
    _write_snapshot_workspace_state(source, plan)
    source_effective_cwd = "/tmp/source-workspace/checkout"
    source_cache_root = "/tmp/source-cache"
    source_checkout_root = "/tmp/source-workspace/checkout"
    source_execution = {
        "cache_root": source_cache_root,
        "workspace_path": "/tmp/source-workspace",
        "checkout_root": source_checkout_root,
        "effective_cwd": source_effective_cwd,
    }
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    source_cache_key = "source-cache-key"
    state_payload["workspace"]["cache_key"] = source_cache_key
    state_payload["workspace"]["cache_root"] = source_cache_root
    state_payload["workspace"]["checkout_root"] = source_checkout_root
    state_payload["execution"] = source_execution
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    frontier = validate_resume_frontier(source, plan)
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    state = json.loads(
        (output.stages_dir / "a" / "workspace-state.json").read_text(encoding="utf-8")
    )
    assert state["run_id"] == output.run_id
    assert state["run_key_name"] == output.run_key_name
    assert state["workspace"]["path"] is None
    assert state["workspace"]["effective_cwd"] is None
    assert state["workspace"]["cache_root"] is None
    assert state["workspace"]["checkout_root"] is None
    assert state["workspace"]["cache_key"] is None
    assert state["execution"]["cache_root"] is None
    assert state["execution"]["workspace_path"] is None
    assert state["execution"]["checkout_root"] is None
    assert state["execution"]["effective_cwd"] is None
    assert state["resume_origin"]["source_run_id"] == "source"
    assert state["resume_origin"]["source_run_key_name"] == "workflow--source"
    assert (
        state["resume_origin"]["source_workspace"]["checkout_root"]
        == source_checkout_root
    )
    assert state["resume_origin"]["source_workspace"]["cache_key"] == source_cache_key
    assert state["resume_origin"]["source_execution"] == source_execution
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    workspace = node_state["workspace"]
    assert workspace["states"][0]["workspace_state_artifact"]["relative_path"] == (
        "a/workspace-state.json"
    )
    assert workspace["states"][0]["resume_origin"]["source_run_id"] == "source"
    assert workspace["states"][0]["workspace"]["materialization"] == "snapshot_checkout"


def test_hydrate_resume_frontier_strips_source_branch_export(
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
    plan = _workspace_snapshot_plan()
    _write_snapshot_workspace_state(source, plan)
    state_path = source.run_dir / "a" / "workspace-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["branch_export"] = {
        "status": "fulfilled",
        "operation": "created",
        "branch_name": "orchestrator/workflow/primary/source",
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    frontier = validate_resume_frontier(source, plan)
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    state = json.loads(
        (output.stages_dir / "a" / "workspace-state.json").read_text(encoding="utf-8")
    )
    assert "branch_export" not in state
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["workspace"]["states"][0].get("branch_export") is None


def test_hydrate_resume_frontier_skips_bool_workspace_artifact_size_bytes(
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
    plan = _workspace_snapshot_plan()
    _write_snapshot_workspace_state(source, plan)
    node_state_path = (
        source.run_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    node_state_payload = json.loads(node_state_path.read_text(encoding="utf-8"))
    state_artifact = node_state_payload["workspace"]["states"][0][
        "workspace_state_artifact"
    ]
    state_artifact["size_bytes"] = True
    node_state_path.write_text(json.dumps(node_state_payload), encoding="utf-8")
    frontier = validate_resume_frontier(source, plan)
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(RuntimeError, match="no workspace-state artifact"):
        hydrate_resume_frontier(frontier, plan, output)


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
    plan = _workspace_worktree_plan()
    stage_dir = source.run_dir / "a"
    _write_lineage_workspace_state(
        stage_dir / "workspace-state-a-alpha-round2.json",
        "2" * 40,
        2,
    )
    _write_lineage_workspace_state(
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
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    assert (
        required_lineage_state_path(output, "a").name
        == "workspace-state-a-alpha-round2.json"
    )
    assert (
        output.stages_dir / "a" / "review-state" / "review-loop-status.json"
    ).is_file()
    assert (output.stages_dir / "a" / "alpha_round2.md").is_file()


@pytest.mark.parametrize(
    "relative_path",
    ("alpha_round2.md", "review-state/review-loop-status.json"),
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
    plan = _workspace_worktree_plan()
    stage_dir = source.run_dir / "a"
    _write_lineage_workspace_state(
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
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="Workspace resume artifact hash changed"):
        hydrate_resume_frontier(frontier, plan, output)


def test_hydrate_resume_frontier_copies_workspace_setup_artifacts(
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
    plan = _workspace_worktree_plan()
    stage_dir = source.run_dir / "a"
    state_path = stage_dir / "workspace-state-a-alpha-round1.json"
    _write_lineage_workspace_state(state_path, "1" * 40, 1)
    metadata_path = stage_dir / "workspace-setup" / f"{state_path.stem}.json"
    log_path = stage_dir / "workspace-setup" / f"{state_path.stem}.log"
    setup = {
        "profile_name": "bootstrap",
        "status": "succeeded",
        "timed_out": False,
        "metadata_path": metadata_path.relative_to(stage_dir).as_posix(),
        "log_path": log_path.relative_to(stage_dir).as_posix(),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(setup), encoding="utf-8")
    log_path.write_text("setup log\n", encoding="utf-8")
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["setup"] = setup
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    node_state_path = (
        source.run_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    node_state = NodeState.model_validate_json(
        node_state_path.read_text(encoding="utf-8")
    )
    frontier = ValidatedResumeFrontier(source, {"a": node_state})
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    hydrated_setup_dir = output.stages_dir / "a" / "workspace-setup"
    assert (hydrated_setup_dir / metadata_path.name).is_file()
    assert (hydrated_setup_dir / log_path.name).read_text(
        encoding="utf-8"
    ) == "setup log\n"


def test_hydrate_resume_frontier_ignores_undeclared_workspace_artifacts(
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
    plan = _workspace_snapshot_plan()
    _write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    extra_state = source.run_dir / "a" / "workspace-state-extra.json"
    extra_state.write_bytes(b"\xff")
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)

    assert (output.stages_dir / "a" / "workspace-state.json").is_file()
    assert not (output.stages_dir / "a" / "workspace-state-extra.json").exists()


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
    output = OutputManager("Workflow", base_dir=tmp_path)

    hydrate_resume_frontier(frontier, plan, output)
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


def test_hydrate_resume_frontier_rechecks_workspace_artifact_hash(
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
    plan = _workspace_snapshot_plan()
    _write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["diagnostics"] = [{"level": "warning", "message": "mutated"}]
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="Workspace resume artifact hash changed"):
        hydrate_resume_frontier(frontier, plan, output)


def _workspace_snapshot_plan():
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="snapshot",
        clean_start="strict",
        materialization="snapshot_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    return plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_source": make_workspace_source_snapshot(),
            "runtime_config_snapshot": {
                "schema_version": SCHEMA_VERSION,
                "invoker": {
                    "implementation": "mock",
                    "capabilities": {
                        "workspace": {
                            "honors_cwd": True,
                            "launch_mode": "mock_no_child_process",
                            "controlled_child_environment": False,
                        }
                    },
                },
            },
        }
    )


def _workspace_worktree_plan():
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    return plan.model_copy(
        update={
            "nodes": [node, plan.nodes[1]],
            "workspace_source": make_workspace_source_snapshot(),
            "runtime_config_snapshot": {
                "schema_version": SCHEMA_VERSION,
                "invoker": {
                    "implementation": "mock",
                    "capabilities": {
                        "workspace": {
                            "honors_cwd": True,
                            "launch_mode": "mock_no_child_process",
                            "controlled_child_environment": False,
                        }
                    },
                },
            },
        }
    )


def _parallel_workspace_snapshot_plan():
    plan = _workspace_snapshot_plan()
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


def _write_lineage_workspace_state(path, result_commit: str, round_num: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "succeeded",
                "role": "executor",
                "task_id": "alpha",
                "round_num": round_num,
                "audit_round_num": None,
                "workspace": {"lineage_producer": True},
                "result": {
                    "result_commit": result_commit,
                    "result_tree": "b" * 40,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_review_status(stage_dir, canonical_path: str) -> None:
    status_dir = stage_dir / "review-state"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "review-loop-status.json").write_text(
        json.dumps(
            {
                "node_id": "a",
                "executed_audit_rounds": 1,
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
                    }
                ],
                "reviewer_outputs": [],
            }
        ),
        encoding="utf-8",
    )


def _write_snapshot_workspace_state(source, plan) -> None:
    payload = snapshot_workspace_state_payload(source, plan, "alpha")
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")


def write_generated_file(
    results_dir,
    relative_path: str,
    content: str,
) -> ArtifactDescriptor:
    path = results_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ArtifactDescriptor(
        kind="generated_file",
        relative_path=relative_path,
        sha256=sha256_hex(content),
        size_bytes=len(content.encode("utf-8")),
    )


def _single_node_plan(node_id: str):
    plan = make_plan()
    node = plan.nodes[0].model_copy(
        update={
            "id": node_id,
            "dependencies": [],
        }
    )
    return plan.model_copy(
        update={
            "execution_order": [node_id],
            "nodes": [node],
            "dependency_graph": [],
        }
    )
