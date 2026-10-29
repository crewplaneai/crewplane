"""Commit foreach artifacts and synchronously install their scheduler graph."""

from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    ExecutionStatus,
    JsonObject,
    NodeArtifactRequest,
    TopologyNode,
    TopologyProvider,
)
from crewplane.architecture.contracts.execution_event import (
    TopologyExpansionPayload,
    emit_event,
)
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.expansions import caller_locators, commit_expansion
from crewplane.core.expansion_state import ExpansionArtifact
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.foreach.expansion import descriptor, instantiate_foreach
from crewplane.core.preflight.foreach.index import projected_index
from crewplane.core.preflight.foreach.models import ExpansionBatch
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.preflight.models import CompiledForeach, PreflightExecutionPlan
from crewplane.core.workflow.collections import (
    COLLECTION_BYTE_LIMIT,
    collection_reference,
    decode_collection,
)
from crewplane.observability.events.builders import topology_expansion_event

from ..common import CompiledRuntimeContext
from .execution_session import WorkflowExecutionSession
from .state import initialize_workflow_execution_state


def expand_group(
    session: WorkflowExecutionSession, group_id: str, output: ArtifactStorePort
) -> None:
    """Validate, publish, and install one ready, unexpanded foreach group.

    Requires a validated plan containing the group's frozen template and an
    artifact store for the same run. Prerequisite nodes must have completed and
    published the caller artifacts required by the template.

    Resolve callers in sorted locator order, validate the complete expansion,
    then publish its collection, items, and commit record with recovery copies.
    Install the effective plan and rebuild scheduling fields in the existing
    state object, retaining prior statuses, running tasks, and node errors.
    Update the existing node lookup before emitting the topology expansion.
    Publication and installation are synchronous with no await boundary, so
    children cannot be dispatched against a partially installed graph.

    Exceptions propagate unchanged. Validation or publication failure leaves
    the scheduler graph uninstalled; files already written and registered are
    retained. Event-sink failure occurs after the new graph is installed.

    Args:
        session: The scheduler-owned execution session to update in place.
        group_id: The ready foreach node to expand exactly once.
        output: The run's verified artifact reader and expansion writer.

    Raises:
        StopIteration: No frozen template matches the group.
        KeyError: A caller node is missing from the session's node lookup.
        ValueError: Artifact evidence, collection, or expansion is invalid.
        OSError: Reading or publishing an artifact fails.
    """
    context = session.runtime_context
    plan = context.plan
    template = next(
        template for template in plan.foreach_templates if template.group_id == group_id
    )
    items, callers = _resolve_expansion_inputs(session, template, output)
    batch = instantiate_foreach(plan, template, items, callers)
    effective = apply_expansion(plan, batch)
    projected_index(effective, template, items)
    _publish_expansion(context, output, batch, items)
    _install_expanded_plan(session, effective)
    emit_event(
        session.telemetry.event_sink,
        topology_expansion_event(
            plan.workflow_name,
            session.telemetry.run_id,
            TopologyExpansionPayload(
                group_id,
                len(items),
                _topology_additions(batch),
                tuple(batch.group_dependencies),
            ),
        ),
    )


def _resolve_expansion_inputs(
    session: WorkflowExecutionSession,
    template: CompiledForeach,
    output: ArtifactStorePort,
) -> tuple[list[JsonObject], dict[str, ExpansionArtifact]]:
    items = template.items
    collection_source = collection_reference(items) if isinstance(items, str) else None
    callers: dict[str, ExpansionArtifact] = {}
    for source, kind in sorted(caller_locators(template)):
        node = session.nodes_by_id[source]
        artifact = output.read_verified_node_artifact(
            NodeArtifactRequest(source, node.artifact_contract),
            kind,
            COLLECTION_BYTE_LIMIT if (source, kind) == collection_source else None,
        )
        callers[f"{source}.{kind}"] = descriptor(
            artifact.path.relative_to(output.results_dir).as_posix(), artifact.payload
        )
        if (source, kind) == collection_source:
            items = decode_collection(
                artifact.payload,
                template.max_items,
                template.group_id,
                str(template.items),
            )
    if isinstance(items, str):
        raise ValueError(
            f"Foreach '{template.group_id}' collection artifact is unavailable"
        )
    return items, callers


def _publish_expansion(
    context: CompiledRuntimeContext,
    output: ArtifactStorePort,
    batch: ExpansionBatch,
    items: list[JsonObject],
) -> None:
    publications = context.runtime_publications

    def register(path: Path) -> None:
        publications.publish(path, file_size_and_sha256(path), recovery_source=path)

    with publications.transaction():
        commit_expansion(output, context.plan, batch, items, register)


def _install_expanded_plan(
    session: WorkflowExecutionSession, effective: PreflightExecutionPlan
) -> None:
    prior = session.state
    rebuilt = initialize_workflow_execution_state(
        effective,
        tuple(
            node_id
            for node_id, status in prior.statuses.items()
            if status == ExecutionStatus.SUCCEEDED
        ),
    )
    rebuilt.statuses.update(prior.statuses)
    rebuilt.ready = [
        node_id
        for node_id in rebuilt.ready
        if rebuilt.statuses[node_id] == ExecutionStatus.PENDING
    ]
    prior.ready = rebuilt.ready
    prior.statuses = rebuilt.statuses
    prior.node_order = rebuilt.node_order
    prior.dependencies_by_node = rebuilt.dependencies_by_node
    prior.remaining_dependencies = rebuilt.remaining_dependencies
    prior.dependents = rebuilt.dependents
    prior.failed_dependencies = {
        node_id: {
            dependency
            for dependency in deps
            if prior.statuses[dependency]
            in {ExecutionStatus.FAILED, ExecutionStatus.BLOCKED}
        }
        for node_id, deps in rebuilt.dependencies_by_node.items()
    }
    session.runtime_context.plan = effective
    session.nodes_by_id.clear()
    session.nodes_by_id.update((node.id, node) for node in effective.nodes)


def _topology_additions(batch: ExpansionBatch) -> tuple[TopologyNode, ...]:
    return tuple(
        TopologyNode(
            node.id,
            node.mode,
            tuple(node.dependencies),
            tuple(
                TopologyProvider(provider.provider, provider.model, provider.role)
                for provider in node.provider_records
            ),
        )
        for node in batch.nodes
    )
