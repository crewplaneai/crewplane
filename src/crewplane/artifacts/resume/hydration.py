from __future__ import annotations

from datetime import datetime

from crewplane.architecture.contracts import JsonObject, NodeArtifactRequest
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    ResumeOrigin,
)
from crewplane.core.preflight.compiled_records import PreflightExecutionNode
from crewplane.core.preflight.models import PreflightExecutionPlan

from ..workspace.node_state import build_node_workspace_descriptor
from .generated_files import copy_generated_file_descriptors
from .validation import (
    ValidatedResumeFrontier,
    required_resume_artifact_paths,
)
from .verified_copy import VerifiedCopyLabels, copy_verified_artifact
from .workspace_hydration import copy_workspace_artifacts


def hydrate_resume_frontier(
    frontier: ValidatedResumeFrontier,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
) -> tuple[str, ...]:
    nodes_by_id = {node.id: node for node in plan.nodes}
    for node_id in frontier.resumed_node_ids:
        node = nodes_by_id[node_id]
        source_state = frontier.node_states[node_id]
        hydrated_at = datetime.now().isoformat()
        hydrated_descriptors = _copy_descriptors(
            source_state,
            frontier,
            output,
            plan,
            node,
        )
        hydrated_generated_files = copy_generated_file_descriptors(
            frontier.source.results_dir,
            output,
            source_state.generated_files,
            node.id,
        )
        copy_workspace_artifacts(frontier, output, node, hydrated_at)
        output.write_node_resume_source(
            NodeArtifactRequest(node.id, node.artifact_contract),
            _resume_source_payload(
                frontier,
                node_id,
                hydrated_descriptors,
                hydrated_at,
            ),
        )
        output.write_node_success_state(
            _hydrated_node_state(
                node,
                plan,
                output,
                hydrated_descriptors,
                hydrated_generated_files,
                frontier,
                hydrated_at,
            )
        )
        output.record_hydrated_resume_node(
            node_id,
            frontier.source.manifest.run_id,
            frontier.source.manifest.run_key_name,
        )
    return frontier.resumed_node_ids


def _copy_descriptors(
    source_state: NodeState,
    frontier: ValidatedResumeFrontier,
    output: ArtifactStorePort,
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
) -> list[ArtifactDescriptor]:
    hydrated: list[ArtifactDescriptor] = []
    source_descriptors = {
        descriptor.kind: descriptor for descriptor in source_state.artifacts
    }
    for kind, relative_path in required_resume_artifact_paths(plan, node).items():
        descriptor = source_descriptors.get(kind)
        if descriptor is None or descriptor.relative_path != relative_path:
            raise ValueError(
                f"Validated resume descriptor missing for node '{node.id}'."
            )
        source_path = contained_regular_file(
            frontier.source.results_dir,
            descriptor.relative_path,
        )
        if source_path is None:
            raise ValueError(f"Resume artifact for node '{node.id}' is not reusable.")
        target_path = output.results_dir / descriptor.relative_path
        hydrated.append(
            copy_verified_artifact(
                source_path,
                target_path,
                descriptor,
                VerifiedCopyLabels(
                    source_artifact="Resume artifact",
                    hydrated_artifact="Hydrated resume artifact",
                    node_id=node.id,
                ),
            )
        )
    return hydrated


def _hydrated_node_state(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    descriptors: list[ArtifactDescriptor],
    generated_files: list[ArtifactDescriptor],
    frontier: ValidatedResumeFrontier,
    hydrated_at: str,
) -> NodeState:
    return NodeState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        plan_schema_version=plan.plan_schema_version,
        workflow_identity=frontier.source.manifest.workflow_identity,
        workflow_name=plan.workflow_name,
        workflow_signature=plan.workflow_signature,
        run_id=output.run_id,
        run_key_name=output.run_key_name,
        node_id=node.id,
        expansion=node.expansion,
        completed_at=hydrated_at,
        artifacts=descriptors,
        generated_files=generated_files,
        workspace=build_node_workspace_descriptor(node, plan, output),
        resume_origin=ResumeOrigin(
            source_run_id=frontier.source.manifest.run_id,
            source_run_key_name=frontier.source.manifest.run_key_name,
            source_node_id=node.id,
            hydrated_at=hydrated_at,
        ),
    )


def _resume_source_payload(
    frontier: ValidatedResumeFrontier,
    node_id: str,
    descriptors: list[ArtifactDescriptor],
    restored_at: str,
) -> JsonObject:
    descriptors_by_kind = {descriptor.kind: descriptor for descriptor in descriptors}
    output_descriptor = descriptors_by_kind.get("output")
    if output_descriptor is None:
        raise ValueError(f"Hydrated resume descriptor missing for node '{node_id}'.")
    payload: JsonObject = {
        "source_run_id": frontier.source.manifest.run_id,
        "source_run_key_name": frontier.source.manifest.run_key_name,
        "source_node_id": node_id,
        "restored_at": restored_at,
        "result_sha256": output_descriptor.sha256,
    }
    findings_descriptor = descriptors_by_kind.get("findings")
    if findings_descriptor is not None:
        payload["findings_sha256"] = findings_descriptor.sha256
    return payload
