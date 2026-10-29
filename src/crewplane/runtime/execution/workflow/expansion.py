from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import (
    ExecutionStatus,
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
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.foreach.expansion import descriptor, instantiate_foreach
from crewplane.core.preflight.foreach.index import projected_index
from crewplane.core.preflight.foreach.reconstruction import apply_expansion
from crewplane.core.workflow.collections import (
    COLLECTION_BYTE_LIMIT,
    collection_reference,
    decode_collection,
)
from crewplane.observability.events.builders import topology_expansion_event

from .execution_session import WorkflowExecutionSession
from .state import initialize_workflow_execution_state


def expand_group(
    session: WorkflowExecutionSession, group_id: str, output: ArtifactStorePort
) -> None:
    context = session.runtime_context
    plan = context.plan
    template = next(
        template for template in plan.foreach_templates if template.group_id == group_id
    )
    items = template.items
    collection_source = collection_reference(items) if isinstance(items, str) else None
    callers = {}
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
                artifact.payload, template.max_items, group_id, str(template.items)
            )
    if isinstance(items, str):
        raise ValueError(f"Foreach '{group_id}' collection artifact is unavailable")
    batch = instantiate_foreach(plan, template, items, callers)
    effective = apply_expansion(plan, batch)
    projected_index(effective, template, items)
    publications = context.runtime_publications

    def register(path: Path) -> None:
        publications.publish(path, file_size_and_sha256(path), recovery_source=path)

    with publications.transaction():
        commit_expansion(output, plan, batch, items, register)
    # Commit and installation have no await boundary: children cannot observe a partial graph.
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
    context.plan = effective
    session.nodes_by_id.clear()
    session.nodes_by_id.update((node.id, node) for node in effective.nodes)
    additions = tuple(
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
    emit_event(
        session.telemetry.event_sink,
        topology_expansion_event(
            plan.workflow_name,
            session.telemetry.run_id,
            TopologyExpansionPayload(
                group_id, len(items), additions, tuple(batch.group_dependencies)
            ),
        ),
    )
