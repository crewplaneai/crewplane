from __future__ import annotations

import hashlib
from datetime import datetime

from orchestrator_cli.architecture.contracts import JsonObject
from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.execution_state import (
    RUN_STATE_SCHEMA_VERSION,
    ArtifactDescriptor,
    NodeState,
    ResumeOrigin,
)
from orchestrator_cli.core.preflight.models import (
    PreflightExecutionNode,
    PreflightExecutionPlan,
)

from .atomic import atomic_write_bytes
from .resume_validation import (
    ValidatedResumeFrontier,
    contained_regular_file,
    required_resume_artifact_paths,
)


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
        output.write_resume_source(
            node_id,
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
                frontier,
                hydrated_at,
            )
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
        payload = source_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != descriptor.sha256:
            raise ValueError(f"Resume artifact hash changed for node '{node.id}'.")
        if len(payload) != descriptor.size_bytes:
            raise ValueError(f"Resume artifact size changed for node '{node.id}'.")
        target_path = output.results_dir / descriptor.relative_path
        atomic_write_bytes(target_path, payload)
        hydrated.append(
            ArtifactDescriptor(
                kind=descriptor.kind,
                relative_path=descriptor.relative_path,
                size_bytes=target_path.stat().st_size,
                sha256=hashlib.sha256(target_path.read_bytes()).hexdigest(),
            )
        )
    return hydrated


def _hydrated_node_state(
    node: PreflightExecutionNode,
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    descriptors: list[ArtifactDescriptor],
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
        completed_at=hydrated_at,
        artifacts=descriptors,
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
