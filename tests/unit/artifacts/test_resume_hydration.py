from __future__ import annotations

import json

import pytest

from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.artifacts.resume_hydration import hydrate_resume_frontier
from orchestrator_cli.artifacts.resume_validation import validate_resume_frontier
from orchestrator_cli.artifacts.run_history import find_same_context_runs
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_node_state,
    make_plan,
    make_run_manifest,
    write_node_state,
    write_result,
    write_run_manifest,
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
