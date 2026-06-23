import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    WorkspaceFileLocator,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.runtime.execution.common import CompiledRuntimeContext
from crewplane.runtime.execution.input import execute_input_stage
from crewplane.version import SCHEMA_VERSION


def _plan(
    context_root: Path,
    node: PreflightExecutionNode,
    workspace_file_locators: list[WorkspaceFileLocator] | None = None,
) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        run_id="run",
        run_key_name="workflow-run",
        project_root=context_root.as_posix(),
        context_root=context_root.as_posix(),
        manifest_root=(context_root / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3).isoformat(),
        workflow_name="workflow",
        workflow_signature="0" * 64,
        execution_order=[node.id],
        nodes=[node],
        render_plans=[],
        static_resources=[],
        workspace_file_locators=list(workspace_file_locators or []),
        token_catalog=[],
        dependency_graph=[],
        runtime_config_snapshot={
            "execution": {},
            "schema_version": SCHEMA_VERSION,
        },
        effective_runtime_config_signature="1" * 64,
        workspace_source=None,
        fingerprint_metadata={"payload_version": "1"},
    )


def _runtime_context(
    context_root: Path,
    node: PreflightExecutionNode,
    workspace_file_locators: list[WorkspaceFileLocator] | None = None,
) -> CompiledRuntimeContext:
    return CompiledRuntimeContext(
        plan=_plan(context_root, node, workspace_file_locators),
        secret_context=SecretContext(),
    )


def _input_node(
    content_ref: str | None = None,
    workspace_locator_id: str | None = None,
) -> PreflightExecutionNode:
    return PreflightExecutionNode(
        id="input.node",
        mode="input",
        artifact_contract=ArtifactContract(output_path="input.node-result.md"),
        input_content_ref=content_ref,
        input_workspace_file_locator_id=workspace_locator_id,
    )


def _static_content_ref(payload: bytes) -> str:
    return f"static-files/{hashlib.sha256(payload).hexdigest()}.txt"


def _workspace_file_locator(payload: bytes) -> WorkspaceFileLocator:
    digest = hashlib.sha256(payload).hexdigest()
    return WorkspaceFileLocator(
        locator_id="workspace-file-input",
        content_ref="workspace-files/workspace-file-input.txt",
        occurrence_id="input.node:input:0",
        node_id="input.node",
        target="input_output",
        source_class="project_initial",
        raw_token="{{file:docs/requirements.md}}",
        raw_path="docs/requirements.md",
        source_root="/repo",
        source_root_relative_to_project=".",
        project_root_relative_to_git_top=".",
        git_top_relative_path="docs/requirements.md",
        workspace_relative_path="docs/requirements.md",
        git_blob="a" * 40,
        git_file_mode="100644",
        byte_size=len(payload),
        canonical_blob_sha256=digest,
        literal_path_verified=True,
        utf8_validated=True,
    )


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
    assert not (output.stages_dir / "input.node" / "workspace-state.json").exists()


def test_input_stage_reads_compiled_workspace_file_locator(tmp_path: Path) -> None:
    context_root = tmp_path / "stages" / "workflow-run"
    payload = b"workspace input"
    locator = _workspace_file_locator(payload)
    workspace_file = context_root / "preflight" / str(locator.content_ref)
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_bytes(payload)
    output = OutputManager("workflow", base_dir=tmp_path / "artifacts")
    node = _input_node(workspace_locator_id=locator.locator_id)

    execute_input_stage(
        node,
        output,
        runtime_context=_runtime_context(context_root, node, [locator]),
    )

    assert (output.stages_dir / "input.node" / "input_round1.md").read_text(
        encoding="utf-8"
    ) == "workspace input"
    assert not (output.stages_dir / "input.node" / "workspace-state.json").exists()


def test_input_stage_rejects_path_traversal_content_ref(tmp_path: Path) -> None:
    context_root = tmp_path / "stages" / "workflow-run"
    output = OutputManager("workflow", base_dir=tmp_path / "artifacts")
    node = _input_node(content_ref="../secret.txt")

    with pytest.raises(ValueError, match="Invalid input content reference"):
        execute_input_stage(
            node,
            output,
            runtime_context=_runtime_context(context_root, node),
        )
