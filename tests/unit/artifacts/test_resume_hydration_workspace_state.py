from __future__ import annotations

import json
from pathlib import Path

import pytest

import crewplane.artifacts.resume.hydration as hydration_module
from crewplane.artifacts.naming import (
    build_node_state_filename,
)
from crewplane.artifacts.resume.validation import (
    validate_resume_frontier,
)
from crewplane.artifacts.run_history import find_same_context_runs
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    attach_workspace_descriptor,
    make_node_state,
    make_run_manifest,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.unit.artifacts.resume_hydration_support import (
    hydration_output,
    workspace_snapshot_plan,
    write_snapshot_workspace_state,
)


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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
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
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

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


def test_hydrate_rechecks_rewritten_workspace_state_before_node_success(
    tmp_path,
    monkeypatch,
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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    output = hydration_output(tmp_path)
    write_bytes = hydration_module.atomic_write_bytes

    def corrupt_workspace_state(path: Path, payload: bytes) -> None:
        write_bytes(path, payload)
        if path.name == "workspace-state.json":
            path.write_bytes(b"x" + payload[1:])

    monkeypatch.setattr(
        hydration_module,
        "atomic_write_bytes",
        corrupt_workspace_state,
    )

    with pytest.raises(
        ValueError,
        match="Hydrated workspace resume artifact changed",
    ):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)

    node_state_path = (
        output.stages_dir / "manifests" / "nodes" / build_node_state_filename("a")
    )
    assert not node_state_path.exists()


def test_workspace_target_verification_does_not_add_full_file_read(
    tmp_path,
    monkeypatch,
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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    output = hydration_output(tmp_path)
    target_path = output.stages_dir / "a" / "workspace-state.json"
    read_bytes = Path.read_bytes
    target_read_count = 0

    def track_full_target_read(path: Path) -> bytes:
        nonlocal target_read_count
        if path == target_path:
            target_read_count += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_full_target_read)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    assert target_path.is_file()
    assert target_read_count <= 1


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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
    state_path = source.run_dir / "a" / "workspace-state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["branch_export"] = {
        "status": "fulfilled",
        "operation": "created",
        "branch_name": "crewplane/workflow/primary/source",
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    frontier = validate_resume_frontier(source, plan)
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
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
    output = hydration_output(tmp_path)

    with pytest.raises(RuntimeError, match="no workspace-state artifact"):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)
