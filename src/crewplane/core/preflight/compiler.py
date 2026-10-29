from __future__ import annotations

from dataclasses import replace

from crewplane.core.config import Config
from crewplane.core.workflow.collections import collection_reference
from crewplane.core.workflow.graph import topological_waves
from crewplane.core.workflow.models import WorkflowPlan

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    append_diagnostic,
    extend_diagnostics,
    has_errors,
)
from .compiled_records import DependencyEdge, PreflightExecutionNode, RenderPlan
from .dependency_edges import append_dependency_edge
from .diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase
from .execution_nodes import compile_execution_node, nodes_with_graph_dependencies
from .foreach.compilation import compilation_source, finish_foreach_preview
from .models import PreflightCompilationPreview
from .plan_contract_artifacts import validate_artifact_contract_uniqueness
from .plan_signatures import (
    effective_runtime_config_signature_for_plan,
    workflow_signature,
)
from .prompt_transport_warnings import collect_prompt_transport_warnings
from .render_plans import (
    apply_env_policy,
    apply_file_policy,
    apply_var_policy,
    compile_render_plan,
)
from .runtime_config import RuntimeConfigSnapshot
from .runtime_config.capture import (
    finalize_fingerprints,
    runtime_snapshot_with_effective_sensitive_paths,
)
from .secrets import FINGERPRINT_PAYLOAD_VERSION
from .source import PreflightWorkflowSource
from .validation import (
    collect_preflight_policy_diagnostics,
    collect_preflight_provider_reference_diagnostics,
    collect_preflight_workflow_reference_diagnostics,
)
from .value_fingerprints import (
    persisted_value_fingerprints,
)
from .variables import build_builtin_template_variables
from .workspace.records import workspace_policy_records
from .workspace.snapshot_guard import append_missing_workspace_snapshot_diagnostic


def compile_preflight_preview(
    source: PreflightWorkflowSource,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
) -> PreflightCompilationPreview:
    try:
        inventory = compilation_source(source, config)
    except ValueError as exc:
        state = CompileState()
        append_diagnostic(
            state,
            PreflightDiagnosticCode.PREFLIGHT_VALIDATION,
            PreflightDiagnosticPhase.VALIDATION,
            str(exc),
        )
        return _build_preview(source, runtime_snapshot, options, state, [])
    options = replace(
        options,
        item_input_ids=frozenset(
            template.item_node_id for template in source.foreach_templates.values()
        ),
    )
    preview = _compile_preflight_inventory(inventory, config, runtime_snapshot, options)
    if preview.has_errors() or not source.foreach_templates:
        return preview
    preview = finish_foreach_preview(source, preview)
    try:
        from .foreach.compilation import validate_literal_expansions

        validate_literal_expansions(preview, options)
    except ValueError as exc:
        state = CompileState()
        append_diagnostic(
            state,
            PreflightDiagnosticCode.PREFLIGHT_VALIDATION,
            PreflightDiagnosticPhase.VALIDATION,
            str(exc),
        )
        preview.diagnostics.extend(state.diagnostics)
    return preview


def _compile_preflight_inventory(
    source: PreflightWorkflowSource,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
) -> PreflightCompilationPreview:
    """Compile a parsed workflow source into the runtime execution contract."""

    workflow = source.workflow
    effective_options = options.with_source_metadata(source)
    state = CompileState()
    _collect_validation_diagnostics(workflow, config, effective_options, state)
    collect_prompt_transport_warnings(config, state)
    execution_order = _execution_order(workflow, state)
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
        )

    variables = build_builtin_template_variables(effective_options.project_root)
    variables.update(effective_options.runtime_variables)
    apply_file_policy(workflow, effective_options, state)
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
        )

    apply_env_policy(workflow, variables, effective_options, state)
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
        )

    apply_var_policy(workflow, variables, effective_options, state)
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
        )

    runtime_snapshot = runtime_snapshot_with_effective_sensitive_paths(
        workflow,
        config,
        runtime_snapshot,
    )
    runtime_snapshot = finalize_fingerprints(
        runtime_snapshot,
        effective_options,
        state,
    )
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
        )

    render_plans, nodes, dependency_graph = _compile_template_plan(
        workflow,
        config,
        runtime_snapshot,
        effective_options,
        state,
    )
    try:
        validate_artifact_contract_uniqueness(nodes)
    except ValueError as exc:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.PREFLIGHT_VALIDATION,
            phase=PreflightDiagnosticPhase.VALIDATION,
            message=str(exc),
        )
    if has_errors(state):
        return _build_preview(
            source,
            runtime_snapshot,
            effective_options,
            state,
            execution_order,
            nodes,
            render_plans,
            dependency_graph,
        )

    value_fingerprints = persisted_value_fingerprints(state)
    runtime_snapshot = runtime_snapshot.model_copy(
        update={
            "effective_runtime_config_signature": (
                effective_runtime_config_signature_for_plan(runtime_snapshot, nodes)
            )
        }
    )
    compiled_workflow_signature = workflow_signature(
        source=source,
        options=effective_options,
        runtime_snapshot=runtime_snapshot,
        render_plans=render_plans,
        static_resources=state.static_resources,
        workspace_file_locators=state.workspace_file_locators,
        token_catalog=state.token_catalog,
        dependency_graph=dependency_graph,
        nodes=nodes,
        value_fingerprints=value_fingerprints,
    )
    return _build_preview(
        source,
        runtime_snapshot,
        effective_options,
        state,
        execution_order,
        nodes,
        render_plans,
        dependency_graph,
        compiled_workflow_signature,
    )


def _compile_template_plan(
    workflow: WorkflowPlan,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
) -> tuple[list[RenderPlan], list[PreflightExecutionNode], list[DependencyEdge]]:
    render_plans: list[RenderPlan] = []
    nodes: list[PreflightExecutionNode] = []
    workspace_records = workspace_policy_records(workflow, config, runtime_snapshot)
    for node in workflow.nodes:
        for dependency in node.needs:
            append_dependency_edge(
                state,
                source_node=dependency,
                target_node=node.id,
                artifact_name=None,
            )
        if node.mode == "foreach" and isinstance(node.items, str):
            producer, artifact = collection_reference(node.items)
            append_dependency_edge(state, producer, node.id, artifact)
        render_plan = compile_render_plan(node, options, state)
        if render_plan is not None:
            render_plans.append(render_plan)
        nodes.append(
            compile_execution_node(
                node,
                config,
                runtime_snapshot,
                options,
                render_plan,
                state,
                workspace_records.get(node.id),
            )
        )
    dependency_graph = list(state.dependency_edges.values())
    nodes = nodes_with_graph_dependencies(nodes, dependency_graph)
    return render_plans, nodes, dependency_graph


def _build_preview(
    source: PreflightWorkflowSource,
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
    execution_order: list[str],
    nodes: list[PreflightExecutionNode] | None = None,
    render_plans: list[RenderPlan] | None = None,
    dependency_graph: list[DependencyEdge] | None = None,
    compiled_workflow_signature: str | None = None,
) -> PreflightCompilationPreview:
    resolved_nodes = nodes or []
    resolved_render_plans = render_plans or []
    resolved_dependency_graph = dependency_graph or []
    value_fingerprints = persisted_value_fingerprints(state)
    workflow = source.workflow
    return PreflightCompilationPreview(
        workflow_name=workflow.name,
        workflow_signature=compiled_workflow_signature,
        execution_order=execution_order,
        nodes=resolved_nodes,
        render_plans=resolved_render_plans,
        static_resources=state.static_resources,
        workspace_file_locators=state.workspace_file_locators,
        token_catalog=state.token_catalog,
        dependency_graph=resolved_dependency_graph,
        diagnostics=state.diagnostics,
        runtime_config_snapshot=runtime_snapshot,
        effective_runtime_config_signature=runtime_snapshot.effective_runtime_config_signature,
        workspace_source=options.workspace_source_snapshot,
        value_fingerprints=value_fingerprints,
        fingerprint_metadata={
            "payload_version": FINGERPRINT_PAYLOAD_VERSION,
            "sensitive_values_required": state.sensitive_values_required,
            "fingerprint_key_persisted": state.fingerprint_key_persisted,
            "persisted_key_path": (
                (options.state_dir / "preflight" / "fingerprint.key").as_posix()
                if state.sensitive_values_required and state.fingerprint_key_persisted
                else None
            ),
        },
        secret_context=state.secret_context,
        static_file_payloads=state.static_payloads,
        workspace_file_payloads=state.workspace_file_payloads,
    )


def _collect_validation_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    extend_diagnostics(
        state, collect_preflight_workflow_reference_diagnostics(workflow)
    )
    extend_diagnostics(
        state,
        collect_preflight_provider_reference_diagnostics(workflow, config),
    )
    extend_diagnostics(state, collect_preflight_policy_diagnostics(workflow, config))
    extend_diagnostics(state, options.additional_diagnostics)
    append_missing_workspace_snapshot_diagnostic(workflow, config, options, state)
    for message in options.additional_validation_errors:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.PROVIDER_CLI,
            phase=PreflightDiagnosticPhase.VALIDATION,
            message=message,
        )
    for message in options.additional_validation_warnings:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.PROVIDER_CLI,
            phase=PreflightDiagnosticPhase.VALIDATION,
            message=message,
            severity="warning",
        )


def _execution_order(workflow: WorkflowPlan, state: CompileState) -> list[str]:
    try:
        return [node_id for wave in topological_waves(workflow) for node_id in wave]
    except ValueError as exc:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.DAG,
            phase=PreflightDiagnosticPhase.REFERENCE,
            message=str(exc),
        )
        return [node.id for node in workflow.nodes]
