"""Verify the retained child run and its binding to host extraction evidence."""

from __future__ import annotations

from pathlib import Path

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.architecture.safe_file_reads import read_contained_bytes
from crewplane.artifacts.memory.evidence import SOURCE_BYTE_LIMIT
from crewplane.artifacts.memory.outcomes import STRUCTURED_LIMIT_BYTES
from crewplane.artifacts.memory.receipts import ExtractionReceipt
from crewplane.artifacts.naming import node_state_relative_path, validate_run_key_name
from crewplane.artifacts.verification import read_verified_node_artifact
from crewplane.core.execution_state import NodeState, RunManifest
from crewplane.core.memory.packets import EvidencePacket
from crewplane.core.preflight.models import PreflightExecutionPlan


def verified_extraction_output(
    state_dir: Path, receipt: ExtractionReceipt, packet: EvidencePacket
) -> str:
    """Read canonical output after the caller confirms receipt completion.

    Require the caller's extraction-attempt lock. This synchronous read performs
    no writes; missing, unsafe, malformed, or mismatched artifacts propagate
    their filesystem or validation errors before any output is returned.
    """
    if receipt.extraction_run_key is None:
        raise ValueError("Extraction receipt has no retained run reference.")
    key = validate_run_key_name(receipt.extraction_run_key)
    stages = state_dir / "execution-stages" / key
    results = state_dir / "execution-results" / key
    plan = _read_matching_child_plan(stages, receipt, key)
    _require_host_packet_binding(plan, packet)
    _require_matching_node_state(stages, plan)
    node = plan.nodes[0]
    return read_verified_node_artifact(
        stages,
        results,
        NodeArtifactRequest(node.id, node.artifact_contract),
        "output",
        SOURCE_BYTE_LIMIT,
        STRUCTURED_LIMIT_BYTES,
    ).payload.decode("utf-8")


def _read_matching_child_plan(
    stages: Path, receipt: ExtractionReceipt, key: str
) -> PreflightExecutionPlan:
    plan = PreflightExecutionPlan.model_validate_json(
        read_contained_bytes(stages, "preflight/execution-plan.json", SOURCE_BYTE_LIMIT)
    )
    manifest = RunManifest.model_validate_json(
        read_contained_bytes(stages, "manifests/run.json", SOURCE_BYTE_LIMIT)
    )
    if (
        plan.run_id != receipt.extraction_run_id
        or manifest.run_id != plan.run_id
        or manifest.status != "succeeded"
        or manifest.workflow_signature != plan.workflow_signature
        or manifest.run_key_name != key
        or plan.run_key_name != key
        or len(plan.nodes) != 1
        or plan.nodes[0].id != "extract"
        or plan.nodes[0].memory
    ):
        raise ValueError(
            "Retained extraction run does not match the successful one-node memory-disabled task."
        )
    return plan


def _require_host_packet_binding(
    plan: PreflightExecutionPlan, packet: EvidencePacket
) -> None:
    evidence_values = [
        fragment.value_stored
        for render in plan.render_plans
        for stream in render.streams
        for fragment in stream.fragments
        if fragment.kind == "static_var" and fragment.key == "memory_evidence"
    ]
    if not evidence_values or any(
        value != packet.model_dump_json(indent=2) for value in evidence_values
    ):
        raise ValueError(
            "Retained extraction input disagrees with the saved host evidence packet."
        )


def _require_matching_node_state(stages: Path, plan: PreflightExecutionPlan) -> None:
    state = NodeState.model_validate_json(
        read_contained_bytes(
            stages,
            node_state_relative_path(plan.nodes[0].id).as_posix(),
            STRUCTURED_LIMIT_BYTES,
        )
    )
    if (
        state.run_id != plan.run_id
        or state.run_key_name != plan.run_key_name
        or state.workflow_signature != plan.workflow_signature
    ):
        raise ValueError(
            "Retained extraction node state identity does not match its child run."
        )
