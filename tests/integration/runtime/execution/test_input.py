import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.core.preflight.secrets import SecretContext
from orchestrator_cli.runtime.execution.common import CompiledRuntimeContext
from orchestrator_cli.runtime.execution.input import execute_input_stage
from orchestrator_cli.version import SCHEMA_VERSION


def _plan(context_root: Path, node: PreflightExecutionNode) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id="run",
        run_key_name="workflow-run",
        context_root=context_root.as_posix(),
        manifest_root=(context_root / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3).isoformat(),
        workflow_name="workflow",
        workflow_signature="0" * 64,
        execution_order=[node.id],
        nodes=[node],
        render_plans=[],
        static_resources=[],
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={
            "execution": {},
            "schema_version": SCHEMA_VERSION,
        },
        effective_runtime_config_signature="1" * 64,
        fingerprint_metadata={"payload_version": "1"},
    )


def _runtime_context(
    context_root: Path,
    node: PreflightExecutionNode,
) -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=_plan(context_root, node),
        secret_context=SecretContext(),
    )


def _input_node(content_ref: str) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="input.node",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.node-result.md"),
        input_content_ref=content_ref,
    )


def _static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def test_input_stage_reads_compiled_preflight_bundle(tmp_path: Path) -> None:
    context_root = tmp_path / "stages" / "workflow-run"
    payload = b"materialized input"
    content_ref = _static_content_ref(payload)
    static_file = context_root / "preflight" / content_ref
    static_file.parent.mkdir(parents=True)
    static_file.write_bytes(payload)
    output = OutputManager("workflow", base_dir=tmp_path / "artifacts")
    node = _input_node(content_ref)

    execute_input_stage(
        node,
        output,
        runtime_context=_runtime_context(context_root, node),
    )

    assert (output.stages_dir / "input.node" / "input_round1.md").read_text(
        encoding="utf-8"
    ) == "materialized input"


def test_input_stage_rejects_path_traversal_content_ref(tmp_path: Path) -> None:
    context_root = tmp_path / "stages" / "workflow-run"
    output = OutputManager("workflow", base_dir=tmp_path / "artifacts")
    node = _input_node("../secret.txt")

    with pytest.raises(ValueError, match="Invalid input content reference"):
        execute_input_stage(
            node,
            output,
            runtime_context=_runtime_context(context_root, node),
        )
