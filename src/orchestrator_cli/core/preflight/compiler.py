from __future__ import annotations

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.workflow_graph import topological_waves
from orchestrator_cli.core.workflow_models import WorkflowPlan

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    append_diagnostic,
    append_multiline_diagnostics,
    has_errors,
)
from .dependency_edges import append_dependency_edge
from .execution_nodes import compile_execution_node, nodes_with_graph_dependencies
from .models import (
    DependencyEdge,
    PreflightCompilationPreview,
    PreflightExecutionNode,
    RenderPlan,
)
from .plan_signatures import workflow_signature
from .render_plans import (
    apply_env_policy,
    apply_file_policy,
    apply_var_policy,
    compile_render_plan,
)
from .runtime_config import RuntimeConfigSnapshot
from .runtime_config_redaction import config_value_handle
from .secrets import FINGERPRINT_SCHEMA_VERSION
from .source import PreflightWorkflowSource
from .validation import (
    validate_preflight_audit_rounds,
    validate_preflight_provider_references,
    validate_preflight_token_budget,
    validate_preflight_workflow_references,
)
from .value_fingerprints import (
    backfill_value_fingerprints,
    load_fingerprint_key_if_needed,
    persisted_value_fingerprints,
)
from .variables import build_builtin_template_variables


def compile_preflight_preview(
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

    runtime_snapshot = _finalize_fingerprints(
        runtime_snapshot, effective_options, state
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
    compiled_workflow_signature = workflow_signature(
        source=source,
        options=effective_options,
        runtime_snapshot=runtime_snapshot,
        render_plans=render_plans,
        static_resources=state.static_resources,
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
    state.render_token_index = 0
    for node in workflow.nodes:
        for dependency in node.needs:
            append_dependency_edge(
                state,
                source_node=dependency,
                target_node=node.id,
                artifact_name=None,
            )
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
            )
        )
    dependency_graph = list(state.dependency_edges.values())
    nodes = nodes_with_graph_dependencies(nodes, dependency_graph)
    return render_plans, nodes, dependency_graph


def _finalize_fingerprints(
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RuntimeConfigSnapshot:
    fingerprinted_snapshot = _fingerprinted_runtime_snapshot(
        runtime_snapshot,
        options,
        state,
    )
    if has_errors(state):
        return fingerprinted_snapshot
    load_fingerprint_key_if_needed(options, state)
    if has_errors(state):
        return fingerprinted_snapshot
    if state.fingerprint_key is not None:
        backfill_value_fingerprints(state)
    _capture_runtime_config_secrets(fingerprinted_snapshot, state)
    return fingerprinted_snapshot


def _capture_runtime_config_secrets(
    runtime_snapshot: RuntimeConfigSnapshot,
    state: CompileState,
) -> None:
    for path in runtime_snapshot.sensitive_config_paths:
        value = _raw_runtime_config_value(runtime_snapshot, path)
        if value is None:
            continue
        state.secret_context.put(config_value_handle(path), str(value))


def _raw_runtime_config_value(
    runtime_snapshot: RuntimeConfigSnapshot,
    path: str,
) -> object | None:
    parts = path.split(".")
    if len(parts) < 2:
        return None
    if parts[0] == "agents":
        return _value_at_path(runtime_snapshot.raw_agents, parts[1:])
    if parts[:3] == ["integrations", "invoker", "options"]:
        raw_invoker = runtime_snapshot.raw_invoker
        return _value_at_path(raw_invoker.options if raw_invoker else {}, parts[3:])
    if parts[:3] == ["integrations", "artifacts", "options"]:
        raw_artifacts = runtime_snapshot.raw_artifacts
        return _value_at_path(
            raw_artifacts.options if raw_artifacts else {},
            parts[3:],
        )
    if parts[:3] == ["integrations", "ui", "options"]:
        raw_ui = runtime_snapshot.raw_ui
        return _value_at_path(raw_ui.options if raw_ui else {}, parts[3:])
    return None


def _value_at_path(payload: object, parts: list[str]) -> object | None:
    current = payload
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


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
        token_catalog=state.token_catalog,
        dependency_graph=resolved_dependency_graph,
        diagnostics=state.diagnostics,
        runtime_config_snapshot=runtime_snapshot,
        effective_runtime_config_signature=runtime_snapshot.effective_runtime_config_signature,
        value_fingerprints=value_fingerprints,
        fingerprint_metadata={
            "schema_version": FINGERPRINT_SCHEMA_VERSION,
            "sensitive_values_required": state.sensitive_values_required,
            "fingerprint_key_persisted": state.fingerprint_key_persisted,
            "persisted_key_path": (
                (options.orchestrator_dir / "preflight" / "fingerprint.key").as_posix()
                if state.sensitive_values_required and state.fingerprint_key_persisted
                else None
            ),
        },
        secret_context=state.secret_context,
        static_file_payloads=state.static_payloads,
    )


def _fingerprinted_runtime_snapshot(
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RuntimeConfigSnapshot:
    if not runtime_snapshot.sensitive_config_paths:
        return runtime_snapshot.with_sensitive_config_fingerprints(None)
    state.sensitive_values_required = True
    load_fingerprint_key_if_needed(options, state)
    if state.fingerprint_key is None:
        return runtime_snapshot
    return runtime_snapshot.with_sensitive_config_fingerprints(state.fingerprint_key)


def _collect_validation_diagnostics(
    workflow: WorkflowPlan,
    config: Config,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    validators = (
        ("reference", validate_preflight_workflow_references),
        (
            "provider",
            lambda candidate: validate_preflight_provider_references(
                candidate,
                config,
            ),
        ),
        (
            "node_policy",
            lambda candidate: validate_preflight_audit_rounds(candidate, config),
        ),
        (
            "node_policy",
            lambda candidate: validate_preflight_token_budget(candidate, config),
        ),
    )
    for phase, validator in validators:
        try:
            validator(workflow)
        except ValueError as exc:
            append_multiline_diagnostics(state, phase, str(exc))
    for message in options.additional_validation_errors:
        append_diagnostic(
            state,
            code="PROVIDER-CLI",
            phase="validation",
            message=message,
        )


def _execution_order(workflow: WorkflowPlan, state: CompileState) -> list[str]:
    try:
        return [node_id for wave in topological_waves(workflow) for node_id in wave]
    except ValueError as exc:
        append_diagnostic(
            state,
            code="DAG",
            phase="reference",
            message=str(exc),
        )
        return [node.id for node in workflow.nodes]
