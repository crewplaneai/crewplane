from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from orchestrator_cli.adapters.invokers.cli import collect_cli_availability_errors
from orchestrator_cli.architecture.errors import IntegrationResolutionError
from orchestrator_cli.architecture.loader import (
    instantiate_adapter,
    resolve_implementation_path,
)
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.artifacts.manager import OutputManager
from orchestrator_cli.bootstrap import (
    RuntimeConfigSnapshotBuildResult,
    build_runtime_config_snapshot,
)
from orchestrator_cli.core.config import Config, Settings
from orchestrator_cli.core.preflight import (
    PREFLIGHT_STATUS_FAILED,
    PREFLIGHT_STATUS_SUCCEEDED,
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
)
from orchestrator_cli.core.preflight.diagnostics import PreflightDiagnostic
from orchestrator_cli.core.preflight.secrets import FingerprintKeyPolicy
from orchestrator_cli.core.preflight.source import PreflightWorkflowSource
from orchestrator_cli.core.workflow_models import WorkflowPlan

from .context import (
    WorkflowRunContext,
    fallback_workflow_name,
    resolve_orchestrator_dir,
    resolve_project_root,
)


def normalize_object_path(implementation: str) -> str:
    if ":" in implementation:
        module_name, object_name = implementation.split(":", 1)
        if module_name and object_name:
            return f"{module_name}:{object_name}"
    elif "." in implementation:
        module_name, object_name = implementation.rsplit(".", 1)
        if module_name and object_name:
            return f"{module_name}:{object_name}"
    return implementation


def uses_cli_invoker(config: Config) -> bool:
    settings = config.settings if config.settings is not None else Settings()
    implementation = settings.integrations.invoker.implementation
    if implementation == "cli":
        return True
    try:
        resolved = resolve_implementation_path("invoker", implementation)
    except IntegrationResolutionError:
        return False
    return normalize_object_path(resolved) == normalize_object_path(
        "orchestrator_cli.adapters.invokers.cli:CliInvokerAdapter"
    )


def compile_workflow_preview(
    config: Config,
    source: PreflightWorkflowSource,
    console: Console,
    no_live: bool,
    fingerprint_key_policy: FingerprintKeyPolicy,
    project_root: Path | None = None,
    orchestrator_dir: Path | None = None,
    check_cli_availability: bool = False,
    which_fn: Callable[[str], str | None] | None = None,
) -> PreflightCompilationPreview:
    resolved_project_root = resolve_project_root(project_root)
    context = WorkflowRunContext(
        config=config,
        source=source,
        console=console,
        project_root=resolved_project_root,
        orchestrator_dir=resolve_orchestrator_dir(
            resolved_project_root,
            orchestrator_dir,
        ),
    )
    snapshot_result = build_runtime_config_snapshot(
        config=config,
        workflow_schema_version=source.workflow.schema_version,
        console=console,
        no_live=no_live,
    )
    return compile_preview(
        context=context,
        snapshot_result=snapshot_result,
        fingerprint_key_policy=fingerprint_key_policy,
        additional_validation_errors=(
            run_cli_availability_errors(source.workflow, config, which_fn)
            if check_cli_availability
            else ()
        ),
    )


def raise_for_preflight_preview_errors(
    preview: PreflightCompilationPreview,
    console: Console,
) -> None:
    print_preflight_diagnostics(preview.diagnostics, console)
    if not preview.has_errors():
        return
    console.print("[red]Preflight compilation failed:[/]")
    for title in preview_error_titles(preview):
        console.print(f"[red]{title}:[/]")
    for diagnostic in preview.diagnostics:
        console.print(f"  - {diagnostic.code}: {diagnostic.message}")
    raise typer.Exit(code=1)


def preview_error_titles(preview: PreflightCompilationPreview) -> tuple[str, ...]:
    titles: list[str] = []
    for diagnostic in preview.diagnostics:
        title = diagnostic_error_title(diagnostic)
        if title is not None and title not in titles:
            titles.append(title)
    return tuple(titles)


def diagnostic_error_title(diagnostic: PreflightDiagnostic) -> str | None:
    if diagnostic.code == "PROVIDER-CLI" or diagnostic.phase == "provider":
        return "Provider validation failed"
    if diagnostic.phase == "node_policy" and "audit_rounds" in diagnostic.message:
        return "Audit rounds validation failed"
    if diagnostic.phase == "node_policy" and "token_budget" in diagnostic.message:
        return "Token budget validation failed"
    return None


def write_early_preflight_failure_run(
    tasks_file: Path | None,
    message: str,
    project_root: Path | None = None,
    orchestrator_dir: Path | None = None,
) -> None:
    """Write a preflight failure bundle when run setup fails before compilation."""

    resolved_project_root = resolve_project_root(project_root)
    path_label = None
    fallback_name = "invalid-workflow"
    if tasks_file is not None:
        fallback_name = fallback_workflow_name(tasks_file)
        path_label = tasks_file.as_posix()
    output = OutputManager(
        fallback_name,
        base_dir=resolve_orchestrator_dir(resolved_project_root, orchestrator_dir),
        template_base_dir=resolved_project_root,
        log_cli_output=False,
    )
    write_preflight_diagnostics(
        output=output,
        diagnostics=[
            PreflightDiagnostic(
                code="PREFLIGHT-SETUP",
                phase="parse",
                path=path_label,
                message=message,
            )
        ],
        workflow_name=None,
    )


def allowed_template_paths(
    snapshot_result: RuntimeConfigSnapshotBuildResult,
) -> tuple[Path, ...]:
    raw_paths = snapshot_result.artifact_options.get("allowed_template_paths", [])
    if not isinstance(raw_paths, list):
        return ()
    return tuple(Path(path) for path in raw_paths if isinstance(path, str))


def compile_preview(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    fingerprint_key_policy: FingerprintKeyPolicy,
    additional_validation_errors: tuple[str, ...] = (),
) -> PreflightCompilationPreview:
    return compile_preflight_preview(
        source=context.source,
        config=context.config,
        runtime_snapshot=snapshot_result.snapshot,
        options=PreflightCompileOptions(
            project_root=context.project_root,
            orchestrator_dir=context.orchestrator_dir,
            allowed_template_paths=allowed_template_paths(snapshot_result),
            fingerprint_key_policy=fingerprint_key_policy,
            additional_validation_errors=additional_validation_errors,
        ),
    )


def run_cli_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    which_fn: Callable[[str], str | None] | None,
) -> tuple[str, ...]:
    if not uses_cli_invoker(config):
        return ()
    return tuple(
        collect_cli_availability_errors(
            workflow,
            config,
            which_fn=which_fn,
        )
    )


def write_preflight_diagnostics(
    output: ArtifactStorePort,
    diagnostics: list[PreflightDiagnostic],
    workflow_name: str | None,
) -> None:
    created_at = datetime.now().isoformat()
    diagnostics_payload = [
        diagnostic.model_dump(mode="json", exclude_none=True)
        for diagnostic in diagnostics
    ]
    output.write_preflight_diagnostics(diagnostics_payload)
    metadata_payload = {
        "created_at": created_at,
        "run_id": output.run_id,
        "run_key_name": output.stages_dir.name,
        "workflow_name": workflow_name,
    }
    output.write_preflight_metadata(metadata_payload)
    manifest_payload = {
        **metadata_payload,
        "diagnostic_count": len(diagnostics),
        "status": PREFLIGHT_STATUS_FAILED,
    }
    output.write_preflight_manifest(manifest_payload)
    summary_lines = [
        "# Preflight Summary",
        "",
        f"- Workflow: {workflow_name or 'unknown'}",
        f"- Run Key: {output.stages_dir.name}",
        f"- Diagnostics: {len(diagnostics)}",
    ]
    for diagnostic in diagnostics:
        summary_lines.append(f"- {diagnostic.code}: {diagnostic.message}")
    output.write_preflight_summary("\n".join(summary_lines) + "\n")


def print_preflight_diagnostics(
    diagnostics: list[PreflightDiagnostic],
    console: Console,
) -> None:
    warnings = [
        diagnostic for diagnostic in diagnostics if diagnostic.severity == "warning"
    ]
    if not warnings:
        return
    console.print("[yellow]Preflight warnings:[/]")
    for diagnostic in warnings:
        console.print(f"  - {diagnostic.code}: {diagnostic.message}")


def write_preflight_success_artifacts(
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
) -> None:
    created_at = datetime.now().isoformat()
    metadata_payload = {
        "created_at": created_at,
        "run_id": output.run_id,
        "run_key_name": output.stages_dir.name,
        "workflow_name": plan.workflow_name,
        "workflow_signature": plan.workflow_signature,
    }
    manifest_payload = {
        **metadata_payload,
        "dependency_count": len(plan.dependency_graph),
        "render_plan_count": len(plan.render_plans),
        "static_resource_count": len(plan.static_resources),
        "status": PREFLIGHT_STATUS_SUCCEEDED,
        "token_count": len(plan.token_catalog),
        "value_fingerprint_count": len(plan.value_fingerprints),
    }
    output.write_preflight_metadata(metadata_payload)
    output.write_preflight_manifest(manifest_payload)
    output.write_preflight_render_plan(plan.render_plans)
    output.write_preflight_json("static-resources.json", plan.static_resources)
    output.write_preflight_json("token-catalog.json", plan.token_catalog)
    output.write_preflight_json("dependency-graph.json", plan.dependency_graph)
    output.write_preflight_execution_bundle(
        {
            "dependency_graph": plan.dependency_graph,
            "render_plans": plan.render_plans,
            "static_resources": plan.static_resources,
            "token_catalog": plan.token_catalog,
            "value_fingerprints": plan.value_fingerprints,
        }
    )
    output.write_preflight_json(
        "runtime-config-snapshot.json",
        plan.runtime_config_snapshot,
    )
    output.write_preflight_summary(
        "\n".join(preflight_success_summary_lines(plan)) + "\n"
    )


def preflight_success_summary_lines(plan: PreflightExecutionPlan) -> list[str]:
    return [
        "# Preflight Summary",
        "",
        f"- Workflow: {plan.workflow_name}",
        f"- Run Key: {plan.run_key_name}",
        f"- Workflow Signature: {plan.workflow_signature}",
        f"- Effective Runtime Config Signature: {plan.effective_runtime_config_signature}",
        "- Execution Plan: preflight/execution-plan.json",
        "- Execution Bundle: preflight/execution-bundle.json",
        f"- Nodes: {len(plan.nodes)}",
        f"- Render Plans: {len(plan.render_plans)}",
        f"- Static Resources: {len(plan.static_resources)}",
        f"- Dependency Edges: {len(plan.dependency_graph)}",
        f"- Tokens: {len(plan.token_catalog)}",
        f"- Value Fingerprints: {len(plan.value_fingerprints)}",
    ]


def write_preflight_failure_artifacts(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    diagnostics: list[PreflightDiagnostic],
    workflow_name: str,
) -> None:
    settings = (
        context.config.settings if context.config.settings is not None else Settings()
    )
    artifacts_adapter = instantiate_adapter(
        "artifacts",
        settings.integrations.artifacts.implementation,
    )
    output = artifacts_adapter.create_store(
        workflow_name=workflow_name,
        orchestrator_dir=context.orchestrator_dir,
        project_root=context.project_root,
        options=snapshot_result.artifact_options,
    )
    write_preflight_diagnostics(output, diagnostics, workflow_name)


def materialize_preflight_success(
    output: ArtifactStorePort,
    preview: PreflightCompilationPreview,
) -> PreflightExecutionPlan:
    created_at = datetime.now()
    run_key_name = output.stages_dir.name
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=output.run_id,
        run_key_name=run_key_name,
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=created_at,
    )
    for content_ref, payload in preview.static_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    output.write_preflight_plan(plan)
    write_preflight_success_artifacts(output, plan)
    return plan
