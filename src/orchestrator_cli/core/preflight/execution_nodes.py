from __future__ import annotations

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.token_budget import resolve_token_budget
from orchestrator_cli.core.workflow_models import ProviderSpec, WorkflowNode

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    module_id,
    node_source_span,
    safe_artifact_name,
    source_file,
    source_root,
)
from .models import (
    ArtifactContract,
    DependencyEdge,
    ExecutionPolicy,
    PreflightExecutionNode,
    ProviderRecord,
    RenderPlan,
)
from .runtime_config import RuntimeConfigSnapshot
from .signatures import signature_for_payload


def compile_execution_node(
    node: WorkflowNode,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
    options: PreflightCompileOptions,
    render_plan: RenderPlan | None,
    state: CompileState,
) -> PreflightExecutionNode:
    policy = ExecutionPolicy(
        depth=node.depth,
        audit_rounds=node.audit_rounds,
        continue_on_failure=node.continue_on_failure,
        failure_threshold=node.failure_threshold,
        token_budget=resolved_token_budget_payload(node, config),
        consensus_on_exhaustion=(
            config.settings.sequential_consensus_on_exhaustion
            if config.settings is not None
            else None
        ),
        concurrency_policy={
            "max_concurrent_nodes": runtime_snapshot.execution.get(
                "max_concurrent_nodes"
            ),
            "max_parallel_invocations": runtime_snapshot.execution.get(
                "max_parallel_invocations"
            ),
        },
    )
    return PreflightExecutionNode(
        id=node.id,
        module_id=module_id(node, options),
        source_file=source_file(node, options),
        source_root=source_root(node, options).as_posix(),
        source_span=node_source_span(node, options),
        mode=node.mode,
        findings=node.findings,
        dependencies=list(node.needs),
        render_plan_id=render_plan.render_plan_id if render_plan is not None else None,
        provider_records=provider_records(node, config, runtime_snapshot),
        execution_policy=policy,
        artifact_contract=ArtifactContract(
            stage_path=node.id,
            output_path=f"{node.id}-result.md",
            findings_path=f"{node.id}-findings.md" if node.findings else None,
            log_path=f"{node.id}/logs",
            result_path=f"{node.id}-result.md",
        ),
        input_content_ref=state.input_content_refs.get(node.id),
    )


def nodes_with_graph_dependencies(
    nodes: list[PreflightExecutionNode],
    dependency_graph: list[DependencyEdge],
) -> list[PreflightExecutionNode]:
    node_order = {node.id: index for index, node in enumerate(nodes)}
    dependencies_by_node = {node.id: set() for node in nodes}
    for edge in dependency_graph:
        dependencies_by_node.setdefault(edge.target_node, set()).add(edge.source_node)
    return [
        node.model_copy(
            update={
                "dependencies": sorted(
                    dependencies_by_node[node.id],
                    key=lambda dependency: node_order.get(dependency, len(node_order)),
                )
            }
        )
        for node in nodes
    ]


def provider_records(
    node: WorkflowNode,
    config: Config,
    runtime_snapshot: RuntimeConfigSnapshot,
) -> list[ProviderRecord]:
    role_indices = {"executor": 0, "reviewer": 0}
    records: list[ProviderRecord] = []
    invoker_signature = signature_for_payload(
        runtime_snapshot.invoker.scoped_payload({"execution", "artifact"})
    )
    for provider in node.providers:
        role_index = role_indices[provider.role]
        role_indices[provider.role] += 1
        agent_config = config.agents.get(provider.provider)
        records.append(
            ProviderRecord(
                provider=provider.provider,
                role=provider.role,
                model=provider.model
                or (agent_config.default_model if agent_config is not None else None),
                task_id=artifact_task_id(provider, role_index),
                agent_config_key=provider.provider,
                invoker_alias=runtime_snapshot.invoker.implementation,
                agent_config_signature=agent_config_signature(
                    provider.provider,
                    runtime_snapshot,
                ),
                invoker_config_signature=invoker_signature,
            )
        )
    return records


def agent_config_signature(
    agent_config_key: str,
    runtime_snapshot: RuntimeConfigSnapshot,
) -> str:
    """Sign the redacted, fingerprinted agent config captured by preflight."""

    return signature_for_payload(
        {
            "agent_config": runtime_snapshot.agents.get(agent_config_key),
            "agent_config_key": agent_config_key,
        }
    )


def resolved_token_budget_payload(
    node: WorkflowNode,
    config: Config,
) -> dict[str, int | None] | None:
    if node.mode == "input":
        return None
    try:
        budget = resolve_token_budget(
            config.settings.token_budget if config.settings is not None else None,
            node.token_budget,
        )
    except ValueError:
        return None
    return {
        "fail_threshold_chars": budget.fail_threshold_chars,
        "warn_threshold_chars": budget.warn_threshold_chars,
    }


def artifact_task_id(provider: ProviderSpec, index: int) -> str:
    return f"{safe_artifact_name(provider.provider)}_{provider.role}_{index}"
