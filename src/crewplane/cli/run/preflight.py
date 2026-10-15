from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from crewplane.architecture.loader import (
    instantiate_adapter,
    is_builtin_implementation,
    resolve_implementation_path,
)
from crewplane.architecture.ports import (
    ArtifactStorePort,
)
from crewplane.artifacts.manager import OutputManager
from crewplane.bootstrap import (
    RuntimeConfigSnapshotBuildResult,
    build_runtime_config_snapshot,
)
from crewplane.core.config import Config
from crewplane.core.preflight import (
    PREFLIGHT_STATUS_FAILED,
    PreflightCompilationPreview,
    PreflightCompileOptions,
    PreflightExecutionPlan,
    compile_preflight_preview,
)
from crewplane.core.preflight.diagnostics import (
    PreflightDiagnostic,
    PreflightDiagnosticCode,
    PreflightDiagnosticPhase,
)
from crewplane.core.preflight.secrets import FingerprintKeyPolicy
from crewplane.core.preflight.source import PreflightWorkflowSource

from .context import (
    WorkflowRunContext,
    build_workflow_run_context,
    fallback_workflow_name,
    resolve_project_root,
    resolve_state_dir,
)
from .invoker_preflight import (
    InvokerDiagnostics,
    collect_invoker_availability_diagnostics,
    collect_invoker_diagnostics,
    invoker_preflight_warning_diagnostics,
)
from .preflight_success import write_preflight_success_artifacts
from .workspace.preflight_diagnostics import workspace_preflight_diagnostics
from .workspace.source_policy import collect_workspace_source_policy

BUILTIN_CLI_INVOKER_IDENTITY = resolve_implementation_path("invoker", "cli")


def uses_cli_invoker(config: Config) -> bool:
    return is_builtin_implementation(
        "invoker", config.settings.integrations.invoker.implementation, "cli"
    )


def uses_mock_invoker(config: Config) -> bool:
    return is_builtin_implementation(
        "invoker", config.settings.integrations.invoker.implementation, "mock"
    )


def compile_workflow_preview(
    config: Config,
    source: PreflightWorkflowSource,
    console: Console,
    no_live: bool,
    fingerprint_key_policy: FingerprintKeyPolicy,
    project_root: Path | None = None,
    state_dir: Path | None = None,
    check_cli_availability: bool = False,
    which_fn: Callable[[str], str | None] | None = None,
    workspace_real_execution: bool = False,
) -> PreflightCompilationPreview:
    context = build_workflow_run_context(
        config, source, console, project_root, state_dir
    )
    snapshot_result = build_runtime_config_snapshot(
        config=config,
        console=console,
        no_live=no_live,
    )
    diagnostics = (
        asyncio.run(
            collect_invoker_availability_diagnostics(
                context.source.workflow,
                config,
                context.project_root,
                snapshot_result.invoker_options,
                which_fn,
            )
        )
        if check_cli_availability
        else collect_invoker_diagnostics(
            context.source.workflow,
            config,
            context.project_root,
            snapshot_result.invoker_options,
        )
    )
    return compile_preview(
        context=context,
        snapshot_result=snapshot_result,
        fingerprint_key_policy=fingerprint_key_policy,
        invoker_diagnostics=diagnostics,
        workspace_real_execution=workspace_real_execution,
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
        if diagnostic.severity != "error":
            continue
        console.print(f"  - {diagnostic.code}: {diagnostic.message}")
    if preview_has_provider_cli_failure(preview):
        console.print(
            "[yellow]Provider setup: docs/getting-started/provider-setup.md[/]"
        )
    raise typer.Exit(code=1)


def preview_error_titles(preview: PreflightCompilationPreview) -> tuple[str, ...]:
    titles: list[str] = []
    for diagnostic in preview.diagnostics:
        if diagnostic.severity != "error":
            continue
        title = diagnostic_error_title(diagnostic)
        if title is not None and title not in titles:
            titles.append(title)
    return tuple(titles)


def preview_has_provider_cli_failure(preview: PreflightCompilationPreview) -> bool:
    return any(
        diagnostic.code == PreflightDiagnosticCode.PROVIDER_CLI
        for diagnostic in preview.diagnostics
    )


def diagnostic_error_title(diagnostic: PreflightDiagnostic) -> str | None:
    if diagnostic.code.startswith("WORKSPACE-"):
        return "Workspace validation failed"
    if (
        diagnostic.code == PreflightDiagnosticCode.PROVIDER_CLI
        or diagnostic.phase == PreflightDiagnosticPhase.PROVIDER
    ):
        return "Provider validation failed"
    if (
        diagnostic.phase == PreflightDiagnosticPhase.NODE_POLICY
        and "audit_rounds" in diagnostic.message
    ):
        return "Audit rounds validation failed"
    if (
        diagnostic.phase == PreflightDiagnosticPhase.NODE_POLICY
        and "token_budget" in diagnostic.message
    ):
        return "Token budget validation failed"
    return None


def write_early_preflight_failure_run(
    tasks_file: Path | None,
    message: str,
    project_root: Path | None = None,
    state_dir: Path | None = None,
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
        base_dir=resolve_state_dir(resolved_project_root, state_dir),
        template_base_dir=resolved_project_root,
        log_cli_output=False,
    )
    write_preflight_diagnostics(
        output=output,
        diagnostics=[
            PreflightDiagnostic(
                code=PreflightDiagnosticCode.PREFLIGHT_SETUP,
                phase=PreflightDiagnosticPhase.PARSE,
                path=path_label,
                message=message,
            )
        ],
        workflow_name=None,
    )


def compile_preview(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    fingerprint_key_policy: FingerprintKeyPolicy,
    additional_validation_errors: tuple[str, ...] = (),
    invoker_diagnostics: InvokerDiagnostics = InvokerDiagnostics(),
    workspace_real_execution: bool = False,
) -> PreflightCompilationPreview:
    workspace_check = collect_workspace_source_policy(
        config=context.config,
        workflow=context.source.workflow,
        project_root=context.project_root,
        state_dir=context.state_dir,
        real_execution=workspace_real_execution,
        invoker_capabilities=snapshot_result.snapshot.invoker.capabilities,
    )
    settings = context.config.settings
    artifacts_adapter = instantiate_adapter(
        "artifacts",
        settings.integrations.artifacts.implementation,
    )
    terminal_history_reader = artifacts_adapter.create_terminal_history_reader(
        context.state_dir,
        snapshot_result.artifact_options,
    )
    return compile_preflight_preview(
        source=context.source,
        config=context.config,
        runtime_snapshot=snapshot_result.snapshot,
        options=PreflightCompileOptions(
            project_root=context.project_root,
            state_dir=context.state_dir,
            allowed_template_paths=tuple(
                Path(path)
                for path in snapshot_result.snapshot.file_access.allowed_template_paths
            ),
            terminal_history_reader=terminal_history_reader,
            fingerprint_key_policy=fingerprint_key_policy,
            additional_validation_errors=(
                invoker_diagnostics.errors + additional_validation_errors
            ),
            additional_diagnostics=(
                workspace_preflight_diagnostics(workspace_check)
                + invoker_preflight_warning_diagnostics(invoker_diagnostics.warnings)
            ),
            workspace_source_snapshot=workspace_check.source_snapshot,
        ),
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
        "run_key_name": output.run_key_name,
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
        f"- Run Key: {output.run_key_name}",
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


def write_preflight_failure_artifacts(
    context: WorkflowRunContext,
    snapshot_result: RuntimeConfigSnapshotBuildResult,
    diagnostics: list[PreflightDiagnostic],
    workflow_name: str,
) -> None:
    settings = context.config.settings
    artifacts_adapter = instantiate_adapter(
        "artifacts",
        settings.integrations.artifacts.implementation,
    )
    output = artifacts_adapter.create_store(
        workflow_name=workflow_name,
        state_dir=context.state_dir,
        project_root=context.project_root,
        options=snapshot_result.artifact_options,
    )
    write_preflight_diagnostics(output, diagnostics, workflow_name)


def materialize_preflight_success(
    output: ArtifactStorePort,
    preview: PreflightCompilationPreview,
    project_root: Path,
) -> PreflightExecutionPlan:
    created_at = datetime.now()
    run_key_name = output.run_key_name
    plan = PreflightExecutionPlan.from_preview(
        preview=preview,
        run_id=output.run_id,
        run_key_name=run_key_name,
        project_root=project_root.as_posix(),
        context_root=output.stages_dir.as_posix(),
        manifest_root=(output.stages_dir / "manifests").as_posix(),
        created_at=created_at,
    )
    for content_ref, payload in preview.static_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    for content_ref, payload in preview.workspace_file_payloads.items():
        output.write_preflight_static_file(content_ref, payload)
    output.write_preflight_plan(plan)
    write_preflight_success_artifacts(output, plan)
    return plan
