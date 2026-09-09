from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from crewplane.architecture.contracts import (
    ArtifactContract,
    NodeArtifactRequest,
    VerifiedNodeArtifact,
)
from crewplane.architecture.ports import ArtifactStorePort
from crewplane.architecture.safe_files import contained_regular_file
from crewplane.core.preflight.models import (
    Fragment,
    PreflightExecutionNode,
    PreflightExecutionPlan,
    RenderStream,
    WorkspaceFileSourceClass,
)
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import (
    FINDINGS_ARTIFACT_KEYS,
    OUTPUT_ARTIFACT_KEYS,
    ProviderRole,
)

from .workspace_files import (
    ResolvedWorkspaceFile,
    resolve_workspace_file,
)
from .workspace_files.source_resolution import WorkspaceCandidateSourceContext


@dataclass(frozen=True)
class RuntimeLocatorInspection:
    node_id: str
    artifact_name: str
    char_count: int


@dataclass(frozen=True)
class ResolvedPrompt:
    text: str
    workspace_files: tuple[ResolvedWorkspaceFile, ...] = ()


def assemble_prompt(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: ProviderRole,
    output: ArtifactStorePort,
    secret_context: SecretContext,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> str:
    return assemble_prompt_details(
        plan,
        node,
        target_role,
        output,
        secret_context,
        workspace_candidate_source=workspace_candidate_source,
        workspace_candidate_context=workspace_candidate_context,
    ).text


def assemble_prompt_details(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: ProviderRole,
    output: ArtifactStorePort,
    secret_context: SecretContext,
    workspace_candidate_source: bool = False,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None = None,
) -> ResolvedPrompt:
    stream = _find_stream(plan, node, target_role)
    text_parts: list[str] = []
    workspace_files: list[ResolvedWorkspaceFile] = []
    for fragment in sorted(stream.fragments, key=lambda item: item.fragment_index):
        text, workspace_file = _resolve_fragment(
            plan,
            fragment,
            output,
            secret_context,
            workspace_candidate_source,
            workspace_candidate_context,
        )
        text_parts.append(text)
        if workspace_file is not None:
            workspace_files.append(workspace_file)
    return ResolvedPrompt("".join(text_parts), tuple(workspace_files))


def inspect_runtime_locators(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: ProviderRole,
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


def stream_has_runtime_dynamic_workspace_locator(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: ProviderRole,
) -> bool:
    stream = _find_stream(plan, node, target_role)
    locator_ids = {
        fragment.locator.get("locator_id")
        for fragment in stream.fragments
        if fragment.kind == "workspace_file_locator"
        and isinstance(fragment.locator, dict)
        and isinstance(fragment.locator.get("locator_id"), str)
    }
    return any(
        locator.locator_id in locator_ids
        and locator.source_class
        in {
            WorkspaceFileSourceClass.RUNTIME_DYNAMIC,
            WorkspaceFileSourceClass.PROJECT_INITIAL_THEN_CANDIDATE,
        }
        for locator in plan.workspace_file_locators
    )


def _find_stream(
    plan: PreflightExecutionPlan,
    node: PreflightExecutionNode,
    target_role: ProviderRole,
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
    workspace_candidate_source: bool,
    workspace_candidate_context: WorkspaceCandidateSourceContext | None,
) -> tuple[str, ResolvedWorkspaceFile | None]:
    match fragment.kind:
        case "literal":
            return fragment.text or "", None
        case "static_file_content":
            if fragment.content_ref is None:
                raise ValueError("Static file fragment is missing content_ref.")
            return _read_static_file(plan, fragment.content_ref), None
        case "workspace_file_locator":
            locator_id = (
                fragment.locator.get("locator_id")
                if fragment.locator is not None
                else None
            )
            if locator_id is None:
                raise ValueError("Workspace file fragment is missing locator_id.")
            resolved = resolve_workspace_file(
                plan,
                output,
                locator_id,
                workspace_candidate_source=workspace_candidate_source,
                workspace_candidate_context=workspace_candidate_context,
            )
            return resolved.text, resolved
        case "static_env" | "static_var":
            if fragment.value_stored is not None:
                return fragment.value_stored, None
            if fragment.value_handle is None:
                raise ValueError("Static value fragment is missing a value handle.")
            return secret_context.get(fragment.value_handle), None
        case "runtime_locator_lookup":
            node_id, artifact_name = _locator_parts(fragment)
            return _resolve_runtime_locator(plan, output, node_id, artifact_name), None


def _read_static_file(plan: PreflightExecutionPlan, content_ref: str) -> str:
    normalized_ref = Path(content_ref)
    if normalized_ref.is_absolute() or ".." in normalized_ref.parts:
        raise ValueError(f"Invalid static content reference '{content_ref}'.")
    path = contained_regular_file(
        Path(plan.context_root) / "preflight",
        normalized_ref.as_posix(),
    )
    if path is None:
        raise ValueError(
            f"Static content reference is missing or unsafe: '{content_ref}'."
        )
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
    artifact = _artifact(plan, output, node_id, artifact_name)
    if artifact_name.endswith("_path"):
        return artifact.path.as_posix()
    if artifact_name.endswith("_size"):
        return str(artifact.size_bytes)
    if artifact_name.endswith("_sha256"):
        return artifact.sha256
    return artifact.payload.decode("utf-8")


def _artifact(
    plan: PreflightExecutionPlan,
    output: ArtifactStorePort,
    node_id: str,
    artifact_name: str,
) -> VerifiedNodeArtifact:
    contract = _artifact_contract(plan, node_id)
    request = NodeArtifactRequest(node_id, contract)
    if artifact_name in OUTPUT_ARTIFACT_KEYS:
        return output.read_verified_node_artifact(request, "output")
    if artifact_name in FINDINGS_ARTIFACT_KEYS:
        if contract.findings_path is None:
            raise ValueError(f"Node '{node_id}' has no findings artifact locator.")
        return output.read_verified_node_artifact(request, "findings")
    raise ValueError(f"Unsupported artifact locator '{node_id}.{artifact_name}'.")


def _artifact_contract(plan: PreflightExecutionPlan, node_id: str) -> ArtifactContract:
    for node in plan.nodes:
        if node.id == node_id:
            return node.artifact_contract
    raise ValueError(f"Compiled plan has no node artifact contract for '{node_id}'.")
