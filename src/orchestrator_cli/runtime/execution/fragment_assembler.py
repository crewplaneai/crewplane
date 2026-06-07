from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from orchestrator_cli.architecture.ports import ArtifactStorePort
from orchestrator_cli.core.preflight.models import (
    ArtifactContract,
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    RenderStream,
)
from orchestrator_cli.core.preflight.secrets import SecretContext

OUTPUT_ARTIFACT_KEYS = {"output", "output_path", "output_size", "output_sha256"}
FINDINGS_ARTIFACT_KEYS = {
    "findings",
    "findings_path",
    "findings_size",
    "findings_sha256",
}


@dataclass(frozen=True)
class RuntimeLocatorInspection:
    node_id: str
    artifact_name: str
    char_count: int


def assemble_prompt(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: str,
    output: ArtifactStorePort,
    secret_context: SecretContext,
) -> str:
    stream = _find_stream(plan, node, target_role)
    return "".join(
        _resolve_fragment(plan, fragment, output, secret_context)
        for fragment in sorted(stream.fragments, key=lambda item: item.fragment_index)
    )


def inspect_runtime_locators(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: str,
    output: ArtifactStorePort,
) -> tuple[RuntimeLocatorInspection, ...]:
    stream = _find_stream(plan, node, target_role)
    inspections: list[RuntimeLocatorInspection] = []
    seen: set[tuple[str, str]] = set()
    for fragment in stream.fragments:
        if fragment.kind != "runtime_locator_lookup" or fragment.locator is None:
            continue
        locator = _locator_parts(fragment)
        if locator in seen:
            continue
        seen.add(locator)
        value = _resolve_runtime_locator(plan, output, locator[0], locator[1])
        inspections.append(
            RuntimeLocatorInspection(
                node_id=locator[0],
                artifact_name=locator[1],
                char_count=len(value),
            )
        )
    return tuple(inspections)


def _find_stream(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: str,
) -> RenderStream:
    if node.render_plan_id is None:
        raise ValueError(f"Node '{node.id}' does not have a render plan.")
    for render_plan in plan.render_plans:
        if render_plan.render_plan_id != node.render_plan_id:
            continue
        for stream in render_plan.streams:
            if stream.target_role == target_role:
                return stream
    raise ValueError(f"Render stream '{target_role}' not found for node '{node.id}'.")


def _resolve_fragment(
    plan: PreflightExecutionPlan,
    fragment: Fragment,
    output: ArtifactStorePort,
    secret_context: SecretContext,
) -> str:
    match fragment.kind:
        case "literal":
            return fragment.text or ""
        case "static_file_content":
            if fragment.content_ref is None:
                raise ValueError("Static file fragment is missing content_ref.")
            return _read_static_file(plan, fragment.content_ref)
        case "static_env" | "static_var":
            if fragment.value_stored is not None:
                return fragment.value_stored
            if fragment.value_handle is None:
                raise ValueError("Static value fragment is missing a value handle.")
            return secret_context.get(fragment.value_handle)
        case "runtime_locator_lookup":
            node_id, artifact_name = _locator_parts(fragment)
            return _resolve_runtime_locator(plan, output, node_id, artifact_name)


def _read_static_file(plan: PreflightExecutionPlan, content_ref: str) -> str:
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid static content reference '{content_ref}'.")
    path = Path(plan.context_root) / "preflight" / normalized_ref
    return path.read_text(encoding="utf-8")


def _locator_parts(fragment: Fragment) -> tuple[str, str]:
    if fragment.locator is None:
        raise ValueError("Runtime locator fragment is missing locator metadata.")
    node_id = fragment.locator.get("node_id")
    artifact_name = fragment.locator.get("artifact_name")
    if not node_id or not artifact_name:
        raise ValueError("Runtime locator fragment has incomplete locator metadata.")
    return node_id, artifact_name


def _resolve_runtime_locator(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    node_id: str,
    artifact_name: str,
) -> str:
    artifact_path = _artifact_path(plan, output, node_id, artifact_name)
    if artifact_name.endswith("_path"):
        return artifact_path.as_posix()
    if artifact_name.endswith("_size"):
        return str(artifact_path.stat().st_size)
    if artifact_name.endswith("_sha256"):
        return hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return artifact_path.read_text(encoding="utf-8")


def _artifact_path(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    node_id: str,
    artifact_name: str,
) -> Path:
    contract = _artifact_contract(plan, node_id)
    if artifact_name in OUTPUT_ARTIFACT_KEYS:
        return _resolve_contract_path(output.results_dir, contract.output_path)
    if artifact_name in FINDINGS_ARTIFACT_KEYS:
        if contract.findings_path is None:
            raise ValueError(f"Node '{node_id}' has no findings artifact locator.")
        return _resolve_contract_path(output.results_dir, contract.findings_path)
    raise ValueError(f"Unsupported artifact locator '{node_id}.{artifact_name}'.")


def _artifact_contract(plan: PreflightExecutionPlan, node_id: str) -> ArtifactContract:
    for node in plan.nodes:
        if node.id == node_id:
            return node.artifact_contract
    raise ValueError(f"Compiled plan has no node artifact contract for '{node_id}'.")


def _resolve_contract_path(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Invalid compiled artifact path '{relative_path}'.")
    return root / path
