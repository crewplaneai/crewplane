from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from crewplane.architecture.errors import (
    AdapterContractError,
    IntegrationResolutionError,
)
from crewplane.architecture.loader import (
    instantiate_adapter,
    resolve_implementation_path,
)
from crewplane.architecture.ports import ArtifactStorePort, InvokerAdapterPort
from crewplane.artifacts.manager import OutputManager
from crewplane.bootstrap import (
    RuntimeConfigSnapshotBuildResult,
    build_runtime_config_snapshot,
)
from crewplane.core.config import Config, Settings
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
from crewplane.core.workflow.models import WorkflowPlan

from .context import (
    WorkflowRunContext,
    fallback_workflow_name,
    resolve_project_root,
    resolve_state_dir,
)
from .preflight_success import write_preflight_success_artifacts
from .workspace.preflight_diagnostics import workspace_preflight_diagnostics
from .workspace.source_policy import collect_workspace_source_policy

BUILTIN_CLI_INVOKER_IDENTITY = resolve_implementation_path("invoker", "cli")


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


def _uses_invoker_implementation(
    config: Config,
    shorthand: str,
    implementation_path: str,
) -> bool:
    settings = config.settings if config.settings is not None else Settings()
    implementation = settings.integrations.invoker.implementation
    if implementation == shorthand:
        return True
    try:
        resolved = resolve_implementation_path("invoker", implementation)
    except IntegrationResolutionError:
        return False
    return normalize_object_path(resolved) == normalize_object_path(implementation_path)


def uses_cli_invoker(config: Config) -> bool:
    return _uses_invoker_implementation(
        config,
        "cli",
        BUILTIN_CLI_INVOKER_IDENTITY,
    )


def uses_mock_invoker(config: Config) -> bool:
    return _uses_invoker_implementation(
        config,
        "mock",
        "crewplane.adapters.invokers.mock:MockInvokerAdapter",
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
    resolved_project_root = resolve_project_root(project_root)
    context = WorkflowRunContext(
        config=config,
        source=source,
        console=console,
        project_root=resolved_project_root,
        state_dir=resolve_state_dir(
            resolved_project_root,
            state_dir,
        ),
    )
    snapshot_result = build_runtime_config_snapshot(
        config=config,
        console=console,
        no_live=no_live,
    )
    return compile_preview(
        context=context,
        snapshot_result=snapshot_result,
        fingerprint_key_policy=fingerprint_key_policy,
        check_invoker_availability=check_cli_availability,
        executable_lookup=which_fn,
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
    check_invoker_availability: bool = False,
    executable_lookup: Callable[[str], str | None] | None = None,
    workspace_real_execution: bool = False,
) -> PreflightCompilationPreview:
    invoker_validation_errors, invoker_validation_warnings = (
        run_invoker_preflight_diagnostics(
            workflow=context.source.workflow,
            config=context.config,
            project_root=context.project_root,
            check_availability=check_invoker_availability,
            executable_lookup=executable_lookup,
        )
    )
    workspace_check = collect_workspace_source_policy(
        config=context.config,
        workflow=context.source.workflow,
        project_root=context.project_root,
        state_dir=context.state_dir,
        real_execution=workspace_real_execution,
        invoker_capabilities=snapshot_result.snapshot.invoker.capabilities,
    )
    return compile_preflight_preview(
        source=context.source,
        config=context.config,
        runtime_snapshot=snapshot_result.snapshot,
        options=PreflightCompileOptions(
            project_root=context.project_root,
            state_dir=context.state_dir,
            allowed_template_paths=allowed_template_paths(snapshot_result),
            fingerprint_key_policy=fingerprint_key_policy,
            additional_validation_errors=(
                invoker_validation_errors + additional_validation_errors
            ),
            additional_diagnostics=(
                workspace_preflight_diagnostics(workspace_check)
                + invoker_preflight_warning_diagnostics(invoker_validation_warnings)
            ),
            workspace_source_snapshot=workspace_check.source_snapshot,
        ),
    )


def run_invoker_preflight_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    project_root: Path,
    check_availability: bool,
    executable_lookup: Callable[[str], str | None] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasoning_locations = _reasoning_requested_locations(workflow)
    settings = config.settings if config.settings is not None else Settings()
    configured_invoker_identity = resolve_implementation_path(
        "invoker",
        settings.integrations.invoker.implementation,
    )
    if not _is_builtin_cli_invoker_identity(configured_invoker_identity):
        return _reasoning_ineligibility_errors(reasoning_locations), ()
    adapter = instantiate_adapter(
        "invoker",
        settings.integrations.invoker.implementation,
    )
    reasoning_errors = run_reasoning_control_errors(
        workflow,
        config,
        adapter,
        configured_invoker_identity,
        project_root,
    )
    availability_errors = (
        run_availability_errors(
            workflow,
            config,
            adapter,
            project_root,
            executable_lookup,
        )
        if check_availability
        else ()
    )
    return (
        reasoning_errors + availability_errors,
        run_model_arg_warnings(config, adapter),
    )


def run_availability_errors(
    workflow: WorkflowPlan,
    config: Config,
    adapter: InvokerAdapterPort,
    project_root: Path,
    executable_lookup: Callable[[str], str | None] | None,
) -> tuple[str, ...]:
    collector = getattr(adapter, "collect_availability_errors", None)
    if not callable(collector):
        return ()
    try:
        errors = collector(
            workflow,
            config,
            project_root,
            executable_lookup=executable_lookup,
        )
    except Exception as exc:
        raise AdapterContractError(
            f"Invoker adapter collect_availability_errors() failed: {exc}"
        ) from exc
    return validated_adapter_messages(errors, "collect_availability_errors")


def run_reasoning_control_errors(
    workflow: WorkflowPlan,
    config: Config,
    adapter: InvokerAdapterPort,
    resolved_identity: str,
    working_directory: Path | None = None,
) -> tuple[str, ...]:
    requested_locations = _reasoning_requested_locations(workflow)
    if not requested_locations:
        return ()
    if not _is_builtin_cli_invoker_identity(resolved_identity):
        return _reasoning_ineligibility_errors(requested_locations)
    collector = getattr(adapter, "collect_reasoning_errors", None)
    if not callable(collector):
        raise AdapterContractError(
            "Built-in CLI invoker adapter must define collect_reasoning_errors()."
        )
    try:
        errors = collector(
            workflow,
            config,
            working_directory=working_directory,
        )
    except Exception as exc:
        raise AdapterContractError(
            f"Built-in CLI invoker collect_reasoning_errors() failed: {exc}"
        ) from exc
    return validated_adapter_messages(errors, "collect_reasoning_errors")


def run_model_arg_warnings(
    config: Config,
    adapter: InvokerAdapterPort,
) -> tuple[str, ...]:
    collector = getattr(adapter, "collect_model_arg_warnings", None)
    if not callable(collector):
        raise AdapterContractError(
            "Built-in CLI invoker adapter must define collect_model_arg_warnings()."
        )
    try:
        warnings = collector(config)
    except Exception as exc:
        raise AdapterContractError(
            f"Built-in CLI invoker collect_model_arg_warnings() failed: {exc}"
        ) from exc
    return validated_adapter_messages(warnings, "collect_model_arg_warnings")


def invoker_preflight_warning_diagnostics(
    warnings: tuple[str, ...],
) -> tuple[PreflightDiagnostic, ...]:
    return tuple(
        PreflightDiagnostic(
            code=PreflightDiagnosticCode.PROVIDER_CONFIG,
            phase=PreflightDiagnosticPhase.PROVIDER,
            message=message,
            severity="warning",
        )
        for message in warnings
    )


def _reasoning_requested_locations(workflow: WorkflowPlan) -> tuple[str, ...]:
    return tuple(
        f"workflow '{workflow.name}' -> node '{node.id}' -> provider "
        f"'{provider.provider}'"
        for node in workflow.nodes
        for provider in node.providers
        if provider.reasoning is not None
    )


def _reasoning_ineligibility_errors(
    requested_locations: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        f"{location}: first-class reasoning requires the built-in CLI invoker."
        for location in requested_locations
    )


def _is_builtin_cli_invoker_identity(resolved_identity: str) -> bool:
    return normalize_object_path(resolved_identity) == normalize_object_path(
        BUILTIN_CLI_INVOKER_IDENTITY
    )


def validated_adapter_messages(value: object, method_name: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
        raise AdapterContractError(
            f"Invoker adapter {method_name}() must return tuple[str, ...]."
        )
    return value


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
    settings = (
        context.config.settings if context.config.settings is not None else Settings()
    )
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
