import hashlib
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

from crewplane.adapters.artifacts.filesystem import FilesystemArtifactsAdapter
from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.errors import AdapterContractError
from crewplane.architecture.loader import require_artifact_store
from crewplane.architecture.ports.artifacts import (
    ArtifactAdapterPort,
    ArtifactStorePort,
    StageTaskSpec,
)
from crewplane.artifacts import OutputManager
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    ArtifactKind,
    NodeState,
)
from crewplane.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    ProviderRecord,
    RenderPlan,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.version import SCHEMA_VERSION


def _plan(context_root: Path) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
        plan_schema_version=SCHEMA_VERSION,
        run_id="run",
        run_key_name=context_root.name,
        project_root=context_root.as_posix(),
        context_root=context_root.as_posix(),
        manifest_root=(context_root / "manifests").as_posix(),
        created_at=datetime(2026, 6, 3).isoformat(),
        workflow_name="workflow",
        workflow_signature="0" * 64,
        execution_order=["build"],
        nodes=[
            PreflightExecutionNode(
                id="build",
                mode="sequential",
                findings=True,
                render_plan_id="build",
                artifact_contract=ArtifactContract(
                    stage_path="custom/build-stage",
                    output_path="custom/build-output.md",
                    findings_path="custom/build-findings.md",
                    log_path="custom/build-stage/logs",
                    result_path="custom/build-output.md",
                ),
                provider_records=[
                    ProviderRecord(
                        provider="mock",
                        role=ProviderRole.EXECUTOR,
                        task_id="mock_executor_0",
                        agent_config_key="mock",
                        invoker_alias="mock",
                        agent_config_signature="agent-signature",
                        invoker_config_signature="invoker-signature",
                    )
                ],
            )
        ],
        render_plans=[RenderPlan(render_plan_id="build", node_id="build")],
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


def test_filesystem_artifact_store_writes_preflight_success_contract(
    tmp_path: Path,
) -> None:
    adapter = FilesystemArtifactsAdapter()
    store = adapter.create_store(
        workflow_name="Workflow",
        state_dir=tmp_path,
        project_root=tmp_path,
        options={"log_cli_output": True},
    )
    plan = _plan(store.stages_dir)

    store.write_preflight_plan(plan)
    store.write_preflight_static_file("static-files/context.txt", b"context")
    store.write_preflight_manifest({"status": "preflight_succeeded"})
    store.write_preflight_metadata({"run_id": store.run_id})
    store.write_preflight_summary("# Summary\n")
    store.write_preflight_render_plan([])
    store.write_preflight_execution_bundle({"nodes": []})

    preflight_dir = store.stages_dir / "preflight"
    assert (
        json.loads((preflight_dir / "execution-plan.json").read_text(encoding="utf-8"))[
            "plan_schema_version"
        ]
        == SCHEMA_VERSION
    )
    assert (preflight_dir / "static-files" / "context.txt").read_text(
        encoding="utf-8"
    ) == "context"
    assert (preflight_dir / "manifest.json").exists()
    assert (preflight_dir / "metadata.json").exists()
    assert (preflight_dir / "summary.md").exists()
    assert (preflight_dir / "render-plans.json").exists()
    assert (preflight_dir / "execution-bundle.json").exists()


def test_artifact_store_contract_has_no_runtime_template_variable_handoff() -> None:
    assert (
        "template_variables"
        not in inspect.signature(ArtifactAdapterPort.create_store).parameters
    )
    assert (
        "template_variables"
        not in inspect.signature(FilesystemArtifactsAdapter.create_store).parameters
    )
    assert "template_variables" not in inspect.signature(OutputManager).parameters


def test_filesystem_output_manager_implements_current_artifact_store_port(
    tmp_path: Path,
) -> None:
    output = OutputManager("Workflow", base_dir=tmp_path)

    assert isinstance(output, ArtifactStorePort)
    assert require_artifact_store(output) is output


def test_artifact_store_loader_rejects_removed_store_contract() -> None:
    with pytest.raises(AdapterContractError, match="ArtifactStorePort"):
        require_artifact_store(object())


def test_filesystem_store_honors_non_derived_node_artifact_contract(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactsAdapter().create_store(
        workflow_name="Workflow",
        state_dir=tmp_path,
        project_root=tmp_path,
        options={"log_cli_output": True},
    )
    plan = _plan(store.stages_dir)
    node = plan.nodes[0]
    request = NodeArtifactRequest(node.id, node.artifact_contract)

    stage_dir = store.create_node_dir(request)
    provider_output = stage_dir / "mock_executor_0_round1.md"
    provider_output.write_text(
        "Implementation complete.\n\n"
        "<!-- findings -->\n- Verify the edge case.\n<!-- /findings -->\n",
        encoding="utf-8",
    )
    finalized = store.finalize_node(
        request,
        findings_enabled=True,
        task_specs=(StageTaskSpec("mock_executor_0", ProviderRole.EXECUTOR),),
        generated_file_detection_enabled=False,
    )

    assert stage_dir == store.stages_dir / "custom/build-stage"
    assert finalized.result_file == store.results_dir / "custom/build-output.md"
    assert finalized.findings_file == store.results_dir / "custom/build-findings.md"
    assert store.get_node_output_path(request) == finalized.result_file
    assert store.get_node_findings_path(request) == finalized.findings_file
    assert store.write_node_resume_source(request, {"source": "run-a"}) == (
        stage_dir / "resume-source.json"
    )

    descriptors = [
        _artifact_descriptor("output", finalized.result_file, store.results_dir),
        _artifact_descriptor("findings", finalized.findings_file, store.results_dir),
    ]
    store.write_node_success_state(
        NodeState(
            run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
            plan_schema_version=SCHEMA_VERSION,
            workflow_identity=".crewplane/workflows/workflow.task.md",
            workflow_name=plan.workflow_name,
            workflow_signature=plan.workflow_signature,
            run_id=store.run_id,
            run_key_name=store.run_key_name,
            node_id=node.id,
            completed_at=datetime(2026, 6, 3, 12, 0).isoformat(),
            artifacts=descriptors,
        )
    )

    assert (
        store.read_verified_node_artifact(request, "output").path
        == finalized.result_file
    )
    assert (
        store.read_verified_node_artifact(request, "findings").path
        == finalized.findings_file
    )


def _artifact_descriptor(
    kind: ArtifactKind,
    path: Path | None,
    results_dir: Path,
) -> ArtifactDescriptor:
    assert path is not None
    payload = path.read_bytes()
    return ArtifactDescriptor(
        kind=kind,
        relative_path=path.relative_to(results_dir).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
