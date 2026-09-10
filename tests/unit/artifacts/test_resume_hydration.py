from __future__ import annotations

import json
from pathlib import Path

import pytest

import crewplane.artifacts.resume.hydration as hydration_module
import crewplane.artifacts.resume.verified_copy as verified_copy_module
from crewplane.artifacts.naming import (
    build_generated_file_result_dir_name,
)
from crewplane.artifacts.resume.validation import (
    validate_resume_frontier,
)
from crewplane.artifacts.run_history import find_same_context_runs
from crewplane.core.execution_state import ArtifactDescriptor
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_node_state,
    make_plan,
    make_run_manifest,
    sha256_hex,
    write_node_state,
    write_result,
    write_run_manifest,
)
from tests.unit.artifacts.resume_hydration_support import (
    hydration_output,
)


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
    output = hydration_output(tmp_path)

    resumed = hydration_module.hydrate_resume_frontier(frontier, plan, output)

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
    output = hydration_output(tmp_path)

    with pytest.raises(ValueError, match="hash changed"):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)


def test_hydrate_rechecks_target_after_descriptor_bound_copy(
    tmp_path,
    monkeypatch,
) -> None:
    frontier, plan = validated_frontier(tmp_path, include_findings=False)
    output = hydration_output(tmp_path)

    def corrupt_target_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload + b"corruption")

    monkeypatch.setattr(
        verified_copy_module,
        "atomic_write_bytes",
        corrupt_target_write,
    )

    with pytest.raises(ValueError, match="Hydrated resume artifact size changed"):
        hydration_module.hydrate_resume_frontier(frontier, plan, output)


def test_hydrate_resume_frontier_records_findings_hash_when_required(tmp_path) -> None:
    frontier, plan = validated_frontier(
        tmp_path,
        include_findings=True,
        findings_edge=True,
    )
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

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
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    hydrated_file = output.results_dir / "generated-files/a/alpha/src/app.txt"
    assert hydrated_file.read_text(encoding="utf-8") == "generated content"
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["generated_files"][0]["relative_path"] == (
        "generated-files/a/alpha/src/app.txt"
    )


def test_hydrate_rechecks_generated_file_target_after_copy(
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
    output_descriptor = write_result(source.results_dir, "a-result.md", "a output")
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
    frontier = validate_resume_frontier(source, make_plan())
    output = hydration_output(tmp_path)
    write_bytes = verified_copy_module.atomic_write_bytes

    def corrupt_target_write(path: Path, payload: bytes) -> None:
        if "generated-files" not in path.parts:
            write_bytes(path, payload)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload + b"corruption")

    monkeypatch.setattr(
        verified_copy_module,
        "atomic_write_bytes",
        corrupt_target_write,
    )

    with pytest.raises(
        ValueError,
        match="Hydrated generated file artifact size changed",
    ):
        hydration_module.hydrate_resume_frontier(frontier, make_plan(), output)


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
    output = hydration_output(tmp_path)

    hydration_module.hydrate_resume_frontier(frontier, plan, output)

    hydrated_file = output.results_dir / relative_path
    assert hydrated_file.read_text(encoding="utf-8") == "generated content"
    node_state_path = next((output.stages_dir / "manifests" / "nodes").glob("*.json"))
    node_state = json.loads(node_state_path.read_text(encoding="utf-8"))
    assert node_state["generated_files"][0]["relative_path"] == relative_path


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
