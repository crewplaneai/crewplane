from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.bootstrap import build_runtime_config_snapshot
from crewplane.core.config import AgentConfig
from crewplane.core.preflight import (
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
    load_workflow_source_for_preflight,
)
from tests.helpers.import_needs import write_import_needs_workflow

from .helpers import mock_config


def _compile(root: Path, workflow: Path) -> PreflightCompilationPreview:
    config = mock_config().model_copy(
        update={"agents": {"alpha": AgentConfig(cli_cmd=["mock"])}}
    )
    snapshot = build_runtime_config_snapshot(config, Console(), no_live=True)
    return compile_preflight_preview(
        source=load_workflow_source_for_preflight(workflow, project_root=root),
        config=config,
        runtime_snapshot=snapshot.snapshot,
        options=PreflightCompileOptions(
            project_root=root,
            state_dir=root / ".crewplane",
            fingerprint_key_policy="read_only",
        ),
    )


def test_import_prerequisites_compile_to_ordering_edges_and_round_trip(
    tmp_path: Path,
) -> None:
    workflow = write_import_needs_workflow(tmp_path)
    preview = _compile(tmp_path, workflow)

    assert not preview.diagnostics
    edges = {
        (edge.source_node, edge.target_node, edge.artifact_name)
        for edge in preview.dependency_graph
    }
    assert {
        ("plan", "doc.review", None),
        ("implement", "doc.review", None),
        ("plan", "doc.audit", None),
        ("implement", "doc.audit", None),
    } <= edges
    nodes = {node.id: node for node in preview.nodes}
    assert nodes["doc.context"].dependencies == []
    assert nodes["doc.review"].dependencies == ["doc.context", "plan", "implement"]
    assert nodes["doc.summarize"].dependencies == ["doc.review", "doc.audit"]
    assert preview.execution_order.index("implement") < preview.execution_order.index(
        "doc.review"
    )
    assert set(preview.static_file_payloads.values()) == {b"Existing documentation.\n"}
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id="run-a",
        run_key_name="import-prerequisites--run-a",
        project_root=tmp_path.as_posix(),
        context_root=(tmp_path / "run-a").as_posix(),
        manifest_root=(tmp_path / "run-a" / "manifests").as_posix(),
        created_at=datetime(2026, 10, 26),
    )
    restored = PreflightExecutionPlan.model_validate_json(plan.model_dump_json())
    assert restored.dependency_graph == preview.dependency_graph
    assert restored.workflow_signature == preview.workflow_signature
    assert {node.id: node.dependencies for node in restored.nodes} == {
        node.id: node.dependencies for node in preview.nodes
    }


def test_effective_import_edges_change_signature_without_changing_prompt_artifacts(
    tmp_path: Path,
) -> None:
    workflow = write_import_needs_workflow(tmp_path, ())
    independent = _compile(tmp_path, workflow)
    write_import_needs_workflow(tmp_path)
    gated = _compile(tmp_path, workflow)

    assert not independent.diagnostics
    assert not gated.diagnostics
    assert independent.workflow_signature != gated.workflow_signature
    assert independent.render_plans == gated.render_plans
    assert independent.token_catalog == gated.token_catalog
    assert independent.static_resources == gated.static_resources


def test_import_prerequisites_do_not_defer_missing_file_inputs(tmp_path: Path) -> None:
    workflow = write_import_needs_workflow(tmp_path)
    (tmp_path / "context.md").unlink()

    preview = _compile(tmp_path, workflow)

    assert preview.has_errors()
    assert any(diagnostic.phase == "file_policy" for diagnostic in preview.diagnostics)


@pytest.mark.parametrize("needs", [("missing",), ("doc.review",), ("doc.summarize",)])
def test_invalid_import_edges_fail_preflight(
    tmp_path: Path, needs: tuple[str, ...]
) -> None:
    workflow = write_import_needs_workflow(tmp_path, needs)

    preview = _compile(tmp_path, workflow)

    assert preview.has_errors()
    assert preview.workflow_signature is None
