from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    RunManifest,
)
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    DependencyEdge,
    ExecutionPolicy,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.version import SCHEMA_VERSION

WORKFLOW_IDENTITY = ".orchestrator/workflows/workflow.task.md"
WORKFLOW_NAME = "Workflow"
WORKFLOW_SIGNATURE = hashlib.sha256(b"workflow").hexdigest()
RUNTIME_SIGNATURE = hashlib.sha256(b"runtime").hexdigest()


def sha256_hex(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def iso_datetime(offset_seconds: int = 0) -> str:
    return (datetime(2026, 6, 9, 12, 0) + timedelta(seconds=offset_seconds)).isoformat()


def make_run_manifest(
    run_id: str,
    run_key_name: str,
    status: str = "running",
    started_offset: int = 0,
    workflow_identity: str = WORKFLOW_IDENTITY,
    workflow_name: str = WORKFLOW_NAME,
    workflow_signature: str = WORKFLOW_SIGNATURE,
) -> RunManifest:
    terminal = status in {"succeeded", "failed", "cancelled"}
    return RunManifest(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=workflow_identity,
        workflow_name=workflow_name,
        workflow_signature=workflow_signature,
        run_id=run_id,
        run_key_name=run_key_name,
        started_at=iso_datetime(started_offset),
        completed_at=iso_datetime(started_offset + 1) if terminal else None,
        status=status,
        effective_runtime_config_signature=RUNTIME_SIGNATURE,
        preflight_plan_path="preflight/execution-plan.json",
        preflight_manifest_path="preflight/manifest.json",
        runtime_config_snapshot_path="preflight/runtime-config-snapshot.json",
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        workflow_source="workflow source",
        composed_workflow={"schema_version": SCHEMA_VERSION, "name": workflow_name},
        failure_message="failed" if status == "failed" else None,
        cancel_reason="cancelled" if status == "cancelled" else None,
    )


def write_run_manifest(orchestrator_dir: Path, manifest: RunManifest) -> Path:
    path = (
        orchestrator_dir
        / "execution-stages"
        / manifest.run_key_name
        / "manifests"
        / "run.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        manifest.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return path


def make_plan(findings_edge: bool = False) -> PreflightExecutionPlan:
    a_contract = ArtifactContract(
        stage_path="a",
        output_path="a-result.md",
        findings_path="a-findings.md",
    )
    b_contract = ArtifactContract(stage_path="b", output_path="b-result.md")
    dependency_edge = DependencyEdge(
        source_node="a",
        target_node="b",
        artifact_name="findings" if findings_edge else "output",
        dependency_signature=sha256_hex("a->b"),
        artifact_key="findings" if findings_edge else "output",
    )
    return PreflightExecutionPlan(
        run_id="current-run",
        run_key_name="workflow--current-run",
        context_root=".orchestrator/execution-stages/workflow--current-run",
        manifest_root=".orchestrator/execution-stages/workflow--current-run/manifests",
        created_at=iso_datetime(),
        workflow_name=WORKFLOW_NAME,
        workflow_signature=WORKFLOW_SIGNATURE,
        execution_order=["a", "b"],
        nodes=[
            PreflightExecutionNode(
                id="a",
                mode="sequential",
                dependencies=[],
                execution_policy=ExecutionPolicy(),
                artifact_contract=a_contract,
            ),
            PreflightExecutionNode(
                id="b",
                mode="sequential",
                dependencies=["a"],
                execution_policy=ExecutionPolicy(),
                artifact_contract=b_contract,
            ),
        ],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[dependency_edge],
        runtime_config_snapshot={"schema_version": SCHEMA_VERSION},
        effective_runtime_config_signature=RUNTIME_SIGNATURE,
        fingerprint_metadata={"payload_version": "1"},
    )


def write_result(
    results_dir: Path, relative_path: str, content: str
) -> ArtifactDescriptor:
    path = results_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return ArtifactDescriptor(
        kind="findings" if relative_path.endswith("-findings.md") else "output",
        relative_path=relative_path,
        sha256=sha256_hex(content),
        size_bytes=len(content.encode("utf-8")),
    )


def make_node_state(
    manifest: RunManifest,
    node_id: str,
    artifacts: list[ArtifactDescriptor],
) -> NodeState:
    return NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=SCHEMA_VERSION,
        workflow_identity=manifest.workflow_identity,
        workflow_name=manifest.workflow_name,
        workflow_signature=manifest.workflow_signature,
        run_id=manifest.run_id,
        run_key_name=manifest.run_key_name,
        node_id=node_id,
        completed_at=iso_datetime(10),
        artifacts=artifacts,
    )


def write_node_state(run_dir: Path, node_state: NodeState) -> Path:
    from orchestrator_cli.artifacts.naming import build_node_state_filename

    path = (
        run_dir / "manifests" / "nodes" / build_node_state_filename(node_state.node_id)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        node_state.model_dump_json(indent=2, exclude_none=True) + "\n",
        encoding="utf-8",
    )
    return path
