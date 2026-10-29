"""Verify foreach children and publish the group's ordered terminal-result index."""

from __future__ import annotations

import posixpath

from crewplane.architecture.contracts import JsonObject, JsonValue, NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.artifacts.expansions import (
    read_expansion_artifact,
    verify_node_expansion,
)
from crewplane.core.file_hashing import file_size_and_sha256
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.foreach.expansion import generated_id
from crewplane.core.preflight.foreach.index import MAX_INDEX_SIZE, terminal_nodes
from crewplane.core.preflight.models import CompiledForeach
from crewplane.core.workflow.collections import (
    COLLECTION_BYTE_LIMIT,
    collection_bytes,
    decode_collection,
)

from ..resume import write_successful_node_state
from .execution_session import WorkflowExecutionSession


def join_group(
    session: WorkflowExecutionSession, group_id: str, output: ArtifactStorePort
) -> None:
    """Publish a ready, expanded foreach group's result index and success state.

    Requires the group's frozen template, committed collection, and successful
    children in the same run. Verify every child's expansion provenance, output,
    and declared findings, including nonterminal children. Include only terminal
    results, preserving collection order and template execution order.

    Write the raw index, then the successful node state in one synchronous
    publication transaction, registering both with recovery copies. The caller
    owns scheduler status transitions; this function returns None.

    Lookup, validation, I/O, and publication failures propagate unchanged.
    Verification finishes before publication; publication failures retain files
    and recovery registrations already written.
    """
    context = session.runtime_context
    plan = context.plan
    state = plan.expansions[group_id]
    template = next(
        template for template in plan.foreach_templates if template.group_id == group_id
    )
    items = decode_collection(
        read_expansion_artifact(
            output.stages_dir, state.collection, COLLECTION_BYTE_LIMIT
        ),
        template.max_items,
        group_id,
    )
    group = session.nodes_by_id[group_id]
    entries = _index_entries(session, template, items, output)
    data = collection_bytes(entries, template.max_items, group_id, "result index")
    _publish_join_result(session, group, output, data)


def _index_entries(
    session: WorkflowExecutionSession,
    template: CompiledForeach,
    items: list[JsonObject],
    output: ArtifactStorePort,
) -> list[JsonObject]:
    group = session.nodes_by_id[template.group_id]
    entries: list[JsonObject] = []
    for index, item in enumerate(items):
        results: list[JsonValue] = []
        for local in template.execution_order:
            node = session.nodes_by_id[generated_id(template, local, index)]
            result: JsonObject = {
                "local_node_id": local.removeprefix(f"{template.namespace}."),
                "node_id": node.id,
            }
            result.update(_verified_child_artifacts(output, node, group))
            if local in terminal_nodes(template):
                results.append(result)
        entries.append({"index": index, "item": item, "results": results})
    return entries


def _verified_child_artifacts(
    output: ArtifactStorePort,
    node: PreflightExecutionNode,
    group: PreflightExecutionNode,
) -> JsonObject:
    verify_node_expansion(output.stages_dir, node)
    artifacts: JsonObject = {}
    for kind in ("output", "findings") if node.findings else ("output",):
        artifact = output.read_verified_node_artifact(
            NodeArtifactRequest(node.id, node.artifact_contract), kind
        )
        if artifact.size_bytes > MAX_INDEX_SIZE:
            raise ValueError("Result index artifact size exceeds unsigned 64-bit limit")
        artifacts[kind] = {
            "path": posixpath.relpath(
                artifact.path.relative_to(output.results_dir).as_posix(),
                posixpath.dirname(group.artifact_contract.output_path) or ".",
            ),
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
        }
    return artifacts


def _publish_join_result(
    session: WorkflowExecutionSession,
    group: PreflightExecutionNode,
    output: ArtifactStorePort,
    data: bytes,
) -> None:
    context = session.runtime_context
    plan = context.plan
    with context.runtime_publications.transaction():
        finalized = output.publish_raw_output(
            NodeArtifactRequest(group.id, group.artifact_contract), data
        )
        context.runtime_publications.publish(
            finalized.result_file,
            file_size_and_sha256(finalized.result_file),
            recovery_source=finalized.result_file,
        )
        path = write_successful_node_state(
            group, plan, output, session.workflow_identity, finalized
        )
        context.runtime_publications.publish(
            path, file_size_and_sha256(path), recovery_source=path
        )
