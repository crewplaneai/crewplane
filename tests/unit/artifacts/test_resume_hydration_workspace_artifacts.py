from __future__ import annotations

import json

import pytest

import crewplane.artifacts.resume.hydration as hydration_module
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


@pytest.mark.parametrize(
    "state_name", ["workspace-state.json", "workspace-state-a-alpha-round1.json"]
)
def test_hydrate_resume_frontier_copies_workspace_setup_artifacts(
    tmp_path,
    state_name,
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
    stage_dir = source.run_dir / "a"
    state_path = stage_dir / state_name
    (stage_dir / "workspace-state.json").rename(state_path)
    setup_stem = "setup" if state_name == "workspace-state.json" else state_path.stem
    metadata_path = stage_dir / "workspace-setup" / f"{setup_stem}.json"
    log_path = stage_dir / "workspace-setup" / f"{setup_stem}.log"
    setup = {
        "profile_name": "bootstrap",
        "status": "succeeded",
        "timed_out": False,
        "metadata_path": metadata_path.relative_to(stage_dir).as_posix(),
        "log_path": log_path.relative_to(stage_dir).as_posix(),
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_bytes = (json.dumps(setup, indent=3) + "\r\n").encode()
    log_bytes = b"setup log\r\n\xff"
    metadata_path.write_bytes(metadata_bytes)
    log_path.write_bytes(log_bytes)
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["setup"] = setup
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    frontier = validate_resume_frontier(source, plan)
    assert frontier.resumed_node_ids == ("a",)
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    hydrated_setup_dir = output.stages_dir / "a" / "workspace-setup"
    assert (hydrated_setup_dir / metadata_path.name).read_bytes() == metadata_bytes
    assert (hydrated_setup_dir / log_path.name).read_bytes() == log_bytes
    hydrated_state = json.loads((output.stages_dir / "a" / state_name).read_bytes())
    assert hydrated_state["run_id"] == output.run_id
    assert hydrated_state["run_key_name"] == output.run_key_name
    assert hydrated_state["resume_origin"]["source_run_id"] == "source"
    assert metadata_path.read_bytes() == metadata_bytes

    output.write_run_manifest(
        make_run_manifest(output.run_id, output.run_key_name, status="failed")
    )
    resumed_source = next(
        record
        for record in find_same_context_runs(
            tmp_path, WORKFLOW_IDENTITY, WORKFLOW_NAME, WORKFLOW_SIGNATURE
        )
        if record.manifest.run_id == output.run_id
    )
    next_frontier = validate_resume_frontier(resumed_source, plan)
    assert next_frontier.resumed_node_ids == ("a",)
    next_output = hydration_output(tmp_path)
    hydration_module.hydrate_resume_frontier(next_frontier, plan, next_output)
    next_setup_dir = next_output.stages_dir / "a" / "workspace-setup"
    assert (next_setup_dir / metadata_path.name).read_bytes() == metadata_bytes
    assert (next_setup_dir / log_path.name).read_bytes() == log_bytes


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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    extra_state = source.run_dir / "a" / "workspace-state-extra.json"
    extra_state.write_bytes(b"\xff")
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    assert (output.stages_dir / "a" / "workspace-state.json").is_file()
    assert not (output.stages_dir / "a" / "workspace-state-extra.json").exists()


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
    plan = workspace_snapshot_plan()
    write_snapshot_workspace_state(source, plan)
    frontier = validate_resume_frontier(source, plan)
    state_path = source.run_dir / "a" / "workspace-state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["diagnostics"] = [{"level": "warning", "message": "mutated"}]
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")
    output = hydration_output(tmp_path)

    with pytest.raises(ValueError, match="Workspace resume artifact hash changed"):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)
