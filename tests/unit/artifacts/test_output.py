from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    NodeArtifactRequest,
    build_findings_filename,
    build_result_filename,
)
from crewplane.artifacts import OutputManager
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    RunManifest,
)
from crewplane.core.preflight.models import (
    ArtifactContract,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request


def _workflow_signature(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _minimal_plan(output: OutputManager) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        project_root=output.base_dir.as_posix(),
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3).isoformat(),
        workflow_name="workflow",
        workflow_signature=_workflow_signature("workflow"),
        execution_order=["build.node"],
        nodes=[
            PreflightExecutionNode(
                id="build.node",
                mode="sequential",
                render_plan_id="build.node",
                artifact_contract=ArtifactContract(
                    stage_path="build.node",
                    output_path="build.node-result.md",
                    log_path="build.node/logs",
                    result_path="build.node-result.md",
                ),
                execution_policy=ExecutionPolicy(),
                provider_records=[
                    ProviderRecord(
                        provider="alpha",
                        role=ProviderRole.EXECUTOR,
                        task_id="alpha_executor_0",
                        agent_config_key="alpha",
                        invoker_alias="mock",
                        agent_config_signature=_workflow_signature("alpha-agent"),
                        invoker_config_signature=_workflow_signature("mock-invoker"),
                    )
                ],
            )
        ],
        render_plans=[RenderPlan(render_plan_id="build.node", node_id="build.node")],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature=_workflow_signature("runtime"),
        fingerprint_metadata={"payload_version": "1"},
    )


def _running_manifest(
    output: OutputManager,
    workflow_signature: str | None = None,
) -> RunManifest:
    return RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=".crewplane/workflows/workflow.task.md",
        workflow_name="workflow",
        workflow_signature=workflow_signature or _workflow_signature("workflow"),
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        started_at=datetime(2026, 6, 3, 12, 0).isoformat(),
        status="running",
        effective_runtime_config_signature=_workflow_signature("runtime"),
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        workflow_source="workflow source",
        composed_workflow={"schema_version": SCHEMA_VERSION, "name": "workflow"},
    )


def test_artifacts_support_symlinked_base_directory_ancestor(tmp_path: Path) -> None:
    temp_root = tmp_path
    real_parent = temp_root / "real-parent"
    real_parent.mkdir()
    alias = temp_root / "alias"
    try:
        alias.symlink_to(real_parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    base_dir = alias / "nested" / "state"
    output = OutputManager("Workflow", base_dir=base_dir)
    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text("alpha", encoding="utf-8")

    output.finalize_node(node_artifact_request("build.node"))

    assert output.base_dir == base_dir.resolve(strict=True)
    assert (output.results_dir / build_result_filename("build.node")).is_file()


def test_legacy_stage_path_and_resume_methods_remain_available(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    resume_path = output.write_node_resume_source(
        node_artifact_request("build.node"),
        {"source": "run-a"},
    )

    assert output.get_node_dir(node_artifact_request("build.node")) == stage_dir
    assert output.get_node_output_path(
        node_artifact_request("build.node")
    ) == output.results_dir / build_result_filename("build.node")
    assert output.get_node_findings_path(
        node_artifact_request("build.node", findings_enabled=True)
    ) == output.results_dir / build_findings_filename("build.node")
    assert resume_path == stage_dir / "resume-source.json"
    assert json.loads(resume_path.read_text(encoding="utf-8")) == {"source": "run-a"}


def test_compiled_stage_directory_rejects_symlink_escape(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    outside = base_dir / "outside"
    outside.mkdir()
    stage_link = output.stages_dir / "build.node"
    try:
        stage_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    request = NodeArtifactRequest(
        "build.node",
        ArtifactContract(
            stage_path="build.node",
            output_path="build.node-result.md",
            log_path="build.node/logs",
            result_path="build.node-result.md",
        ),
    )
    with pytest.raises(ValueError, match="real directory"):
        output.create_node_dir(request)


def test_run_allocation_does_not_create_results_until_finalization(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)

    assert output.stages_dir.exists()
    assert not (base_dir / "execution-results").exists()

    stage_dir = output.create_node_dir(node_artifact_request("build.node"))
    (stage_dir / "alpha_round1.md").write_text("alpha", encoding="utf-8")
    output.finalize_node(node_artifact_request("build.node"))

    assert output.results_dir.exists()


def test_compiled_result_path_rejects_symlinked_results_root(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    outside = base_dir / "outside"
    outside.mkdir()
    results_root = base_dir / "execution-results"
    try:
        results_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="real directory"):
        output.get_node_output_path(node_artifact_request("build.node"))

    assert not (outside / output.run_key_name).exists()


def test_compiled_result_path_rejects_symlinked_target(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    outside = base_dir / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    result_path = output.results_dir / build_result_filename("build.node")
    result_path.parent.mkdir(parents=True)
    try:
        result_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="must not be a symlink"):
        output.get_node_output_path(node_artifact_request("build.node"))

    assert outside.read_text(encoding="utf-8") == "outside"


def test_stage_names_do_not_escape_or_collide_after_normalization(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    escaped_candidate = output.create_node_dir(node_artifact_request("..-"))
    dashed = output.create_node_dir(node_artifact_request("-a"))
    plain = output.create_node_dir(node_artifact_request("a"))

    assert escaped_candidate.resolve().is_relative_to(output.stages_dir)
    assert dashed != plain
    assert output.results_dir / build_result_filename(
        "-a"
    ) != output.results_dir / build_result_filename("a")


def test_write_and_update_run_manifest(tmp_path: Path) -> None:
    base_dir = tmp_path
    output = OutputManager("Workflow", base_dir=base_dir)
    manifest = _running_manifest(output)

    output.write_run_manifest(manifest)
    output.update_run_manifest_status(
        "succeeded",
        datetime(2026, 6, 3, 12, 1).isoformat(),
    )

    manifest_path = output.stages_dir / "manifests" / "run.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "succeeded"
    assert payload["workflow_signature"] == manifest.workflow_signature
    assert payload["run_key_name"] == output.run_key_name


def test_write_node_success_state_uses_bounded_manifest_filename(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    node_state = NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=".crewplane/workflows/workflow.task.md",
        workflow_name="workflow",
        workflow_signature=_workflow_signature("workflow"),
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        node_id="build.node",
        completed_at=datetime(2026, 6, 3, 12, 0).isoformat(),
        artifacts=[
            ArtifactDescriptor(
                kind="output",
                relative_path="build.node-result.md",
                sha256=_workflow_signature("result"),
                size_bytes=6,
            )
        ],
    )

    path = output.write_node_success_state(node_state)

    assert path.name == "build.node--811a9309e00c.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["node_id"] == "build.node"


def test_write_preflight_plan_and_static_file(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    plan = _minimal_plan(output)

    static_path = output.write_preflight_static_file(
        "static-files/context.txt",
        b"context",
    )
    plan_path = output.write_preflight_plan(plan)
    manifest_path = output.write_preflight_manifest({"status": "preflight_succeeded"})
    diagnostics_path = output.write_preflight_diagnostics([])
    metadata_path = output.write_preflight_metadata({"run_id": output.run_id})
    render_path = output.write_preflight_render_plan([])
    bundle_path = output.write_preflight_execution_bundle({"nodes": []})
    summary_path = output.write_preflight_summary("# Preflight\n")

    assert static_path.read_text(encoding="utf-8") == "context"
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan_payload["workflow_signature"] == plan.workflow_signature
    assert plan_payload["plan_schema_version"] == SCHEMA_VERSION
    assert "schema_version" not in plan_payload
    assert plan_payload["run_key_name"] == output.run_key_name
    assert manifest_path.name == "manifest.json"
    assert diagnostics_path.name == "diagnostics.json"
    assert metadata_path.name == "metadata.json"
    assert render_path.name == "render-plans.json"
    assert bundle_path.name == "execution-bundle.json"
    assert summary_path.read_text(encoding="utf-8") == "# Preflight\n"


def test_preflight_and_workspace_exports_reject_symlinked_directories(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    preflight_link = output.stages_dir / "preflight"
    export_link = output.stages_dir / "workspace-exports"
    try:
        preflight_link.symlink_to(outside, target_is_directory=True)
        export_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="real directory"):
        output.write_preflight_static_file("static-files/context.txt", b"x")
    with pytest.raises(ValueError, match="real directory"):
        output.write_workspace_export("primary", {"status": "succeeded"})
    assert not (outside / "static-files" / "context.txt").exists()
    assert not (outside / "primary.json").exists()


def test_run_manifest_signature_must_be_sha256_hex(tmp_path: Path) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    with pytest.raises(ValueError, match="workflow_signature"):
        _running_manifest(
            output,
            workflow_signature="not-a-signature",
        )
