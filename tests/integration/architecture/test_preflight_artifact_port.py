import inspect
import json
from datetime import datetime
from pathlib import Path

from orchestrator_cli.adapters.artifacts.filesystem import FilesystemArtifactsAdapter
from orchestrator_cli.architecture.ports.artifacts import ArtifactAdapterPort
from orchestrator_cli.artifacts import OutputManager
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    PreflightExecutionNode,
    PreflightExecutionPlan,
)
from orchestrator_cli.version import SCHEMA_VERSION


def _plan(context_root: Path) -> PreflightExecutionPlan:
    return PreflightExecutionPlan(
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
                artifact_contract=ArtifactContract(output_path="build-result.md"),
            )
        ],
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


def test_filesystem_artifact_store_writes_preflight_success_contract(
    tmp_path: Path,
) -> None:
    adapter = FilesystemArtifactsAdapter()
    store = adapter.create_store(
        workflow_name="Workflow",
        orchestrator_dir=tmp_path,
        project_root=tmp_path,
        options={"allowed_template_paths": [], "log_cli_output": True},
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
