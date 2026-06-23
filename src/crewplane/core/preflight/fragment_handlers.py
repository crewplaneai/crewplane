from __future__ import annotations

import os
import re

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import (
    ALLOWED_NODE_ARTIFACT_NAME_SET,
    ProviderRole,
)
from crewplane.core.workflow.models import WorkflowNode

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    ResolvedStaticFileReference,
    ResolvedStaticValueReference,
    allowed_template_paths,
    append_diagnostic,
    extend_diagnostics,
    source_root,
    token_source_span,
)
from .dependency_edges import append_dependency_edge, dependency_signature
from .diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase
from .models import Fragment
from .references import TemplateReference
from .signatures import signature_for_payload
from .static_resources import append_static_resource, resolve_static_file
from .token_catalog import append_token_catalog
from .workspace.files.locators import (
    token_signature_for_workspace_locator,
    workspace_locator_metadata,
    workspace_locator_resolved_payload,
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|credential|private)",
    re.IGNORECASE,
)


def node_reference_fragment(
    node: WorkflowNode,
    target_role: ProviderRole,
    source_role: PromptSegmentRole,
    segment_index: int,
    reference: TemplateReference,
    options: PreflightCompileOptions,
    state: CompileState,
    fragment_index: int,
    occurrence_id: str,
) -> Fragment | None:
    artifact_name = reference.artifact_name or ""
    target_node = reference.node_id or ""
    if artifact_name not in ALLOWED_NODE_ARTIFACT_NAME_SET:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.NODE_REFERENCE,
            phase=PreflightDiagnosticPhase.REFERENCE,
            node_id=node.id,
            message=f"Unsupported node artifact token '{reference.raw_token}'.",
        )
        return None
    edge_signature = dependency_signature(
        source_node=target_node,
        target_node=node.id,
        artifact_name=artifact_name,
    )
    signature = signature_for_payload(
        {
            "dependency_signature": edge_signature,
            "occurrence_id": occurrence_id,
            "raw_token": reference.raw_token,
            "source": node.id,
            "source_role": source_role,
            "target_role": target_role,
        }
    )
    append_token_catalog(
        state=state,
        occurrence_id=occurrence_id,
        node=node,
        target_role=target_role,
        source_role=source_role,
        reference=reference,
        token_kind="node",
        fragment_index=fragment_index,
        signature=signature,
        options=options,
        source_span=token_source_span(node, options, segment_index, reference),
        dependency_signature=edge_signature,
        canonical_locator=f"{target_node}.{artifact_name}",
        resolved={
            "kind": "runtime_locator_lookup",
            "locator": {"node_id": target_node, "artifact_name": artifact_name},
            "artifact_key": artifact_name,
            "dependency_signature": edge_signature,
        },
        metadata={
            "node_id": target_node,
            "artifact_name": artifact_name,
            "dependency_signature": edge_signature,
        },
    )
    append_dependency_edge(
        state,
        source_node=target_node,
        target_node=node.id,
        artifact_name=artifact_name,
        first_token_signature=signature,
    )
    return Fragment(
        fragment_index=fragment_index,
        kind="runtime_locator_lookup",
        source_role=source_role,
        locator={"node_id": target_node, "artifact_name": artifact_name},
    )


def file_reference_fragment(
    node: WorkflowNode,
    target_role: ProviderRole,
    source_role: PromptSegmentRole,
    segment_index: int,
    reference: TemplateReference,
    options: PreflightCompileOptions,
    state: CompileState,
    fragment_index: int,
    occurrence_id: str,
) -> Fragment | None:
    workspace_locator = state.workspace_file_references.get(occurrence_id)
    if workspace_locator is not None:
        signature = token_signature_for_workspace_locator(workspace_locator)
        append_token_catalog(
            state=state,
            occurrence_id=occurrence_id,
            node=node,
            target_role=target_role,
            source_role=source_role,
            reference=reference,
            token_kind="file",
            fragment_index=fragment_index,
            signature=signature,
            options=options,
            source_span=token_source_span(node, options, segment_index, reference),
            canonical_locator=workspace_locator.locator_id,
            resolved=workspace_locator_resolved_payload(workspace_locator),
            metadata=workspace_locator_metadata(workspace_locator),
        )
        return Fragment(
            fragment_index=fragment_index,
            kind="workspace_file_locator",
            source_role=source_role,
            locator={
                "locator_id": workspace_locator.locator_id,
                "source_class": workspace_locator.source_class,
                "workspace_relative_path": workspace_locator.workspace_relative_path,
            },
        )
    resolution = state.static_file_references.get(occurrence_id)
    if resolution is None:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.TEMPLATE_PLAN,
            phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
            node_id=node.id,
            message=f"File token '{reference.raw_token}' was not resolved.",
        )
        return None
    resource = resolution.resource
    signature = resolution.token_signature
    append_token_catalog(
        state=state,
        occurrence_id=occurrence_id,
        node=node,
        target_role=target_role,
        source_role=source_role,
        reference=reference,
        token_kind="file",
        fragment_index=fragment_index,
        signature=signature,
        options=options,
        source_span=token_source_span(node, options, segment_index, reference),
        canonical_locator=resource.content_ref,
        resolved={
            "kind": "static_file_content",
            "content_ref": resource.content_ref,
            "content_sha256": resource.sha256,
            "content_size": str(resource.size_bytes),
            "resolved_path": resource.resolved_path,
            "source_root": resource.source_root,
        },
        metadata={
            "content_ref": resource.content_ref,
            "sha256": resource.sha256,
        },
    )
    return Fragment(
        fragment_index=fragment_index,
        kind="static_file_content",
        source_role=source_role,
        content_ref=resource.content_ref,
    )


def resolve_file_reference(
    node: WorkflowNode,
    reference: TemplateReference,
    options: PreflightCompileOptions,
    state: CompileState,
    occurrence_id: str,
) -> None:
    result = resolve_static_file(
        raw_path=reference.key or "",
        source_root=source_root(node, options),
        project_root=options.project_root.resolve(),
        allowed_paths=allowed_template_paths(options),
    )
    extend_diagnostics(state, result.diagnostics)
    if result.resource is None or result.payload is None:
        return
    signature = signature_for_payload(
        {
            "content_ref": result.resource.content_ref,
            "node_id": node.id,
            "occurrence_id": occurrence_id,
            "raw_token": reference.raw_token,
            "sha256": result.resource.sha256,
            "size_bytes": result.resource.size_bytes,
        }
    )
    resource = result.resource.model_copy(update={"token_signatures": [signature]})
    append_static_resource(state, resource, result.payload, signature)
    state.static_file_references[occurrence_id] = ResolvedStaticFileReference(
        resource=resource,
        token_signature=signature,
    )


def static_value_fragment(
    node: WorkflowNode,
    target_role: ProviderRole,
    source_role: PromptSegmentRole,
    segment_index: int,
    reference: TemplateReference,
    options: PreflightCompileOptions,
    state: CompileState,
    fragment_index: int,
    occurrence_id: str,
) -> Fragment | None:
    resolution = state.static_value_references.get(occurrence_id)
    if resolution is None:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.TEMPLATE_PLAN,
            phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
            node_id=node.id,
            message=f"Static value token '{reference.raw_token}' was not resolved.",
        )
        return None
    metadata = {
        "key": resolution.key,
        "sensitive": str(resolution.sensitive).lower(),
    }
    resolved_value = static_value_resolved_payload(resolution)
    append_token_catalog(
        state=state,
        occurrence_id=occurrence_id,
        node=node,
        target_role=target_role,
        source_role=source_role,
        reference=reference,
        token_kind=resolution.kind,
        fragment_index=fragment_index,
        signature=resolution.token_signature,
        options=options,
        source_span=token_source_span(node, options, segment_index, reference),
        canonical_locator=f"{resolution.kind}:{resolution.key}",
        resolved=resolved_value,
        metadata=metadata,
    )
    return Fragment(
        fragment_index=fragment_index,
        kind="static_env" if resolution.kind == "env" else "static_var",
        source_role=source_role,
        key=resolution.key,
        value_handle=resolution.value_handle,
        value_stored=resolution.value_stored,
        fingerprint=resolution.fingerprint,
    )


def static_value_resolved_payload(
    resolution: ResolvedStaticValueReference,
) -> dict[str, str]:
    payload = {
        "kind": "static_env" if resolution.kind == "env" else "static_var",
        "key": resolution.key,
    }
    if resolution.value_handle is not None:
        if resolution.fingerprint is None:
            raise ValueError(
                f"Sensitive {resolution.kind} token '{resolution.key}' is missing a fingerprint."
            )
        payload["value_handle"] = resolution.value_handle
        payload["fingerprint"] = resolution.fingerprint
        return payload
    if resolution.value_stored is None:
        raise ValueError(
            f"Static {resolution.kind} token '{resolution.key}' is missing a stored value."
        )
    payload["value_stored"] = resolution.value_stored
    return payload


def resolve_static_value_reference(
    node: WorkflowNode,
    reference: TemplateReference,
    variables: dict[str, str],
    options: PreflightCompileOptions,
    state: CompileState,
    occurrence_id: str,
) -> None:
    key = reference.key or ""
    value = lookup_static_value(reference.kind, key, variables, options, state, node.id)
    if value is None:
        return
    sensitive = (
        reference.kind == "env" or _SENSITIVE_KEY_PATTERN.search(key) is not None
    )
    if sensitive:
        state.sensitive_values_required = True
    handle = f"{reference.kind}:{key}" if sensitive else None
    if handle is not None:
        state.secret_context.put(handle, value)
    if sensitive:
        state.value_fingerprints.append(
            {
                "kind": reference.kind,
                "key": key,
                "sensitive": "true",
                "value": value,
            }
        )
    state.static_value_references[occurrence_id] = ResolvedStaticValueReference(
        kind=reference.kind,
        key=key,
        sensitive=sensitive,
        value_handle=handle,
        value_stored=None if sensitive else value,
        fingerprint=None,
        token_signature=signature_for_payload(
            {
                "key": key,
                "kind": reference.kind,
                "node_id": node.id,
                "occurrence_id": occurrence_id,
                "raw_token": reference.raw_token,
                "sensitive": str(sensitive).lower(),
            }
        ),
    )


def lookup_static_value(
    kind: str,
    key: str,
    variables: dict[str, str],
    options: PreflightCompileOptions,
    state: CompileState,
    node_id: str,
) -> str | None:
    phase = static_value_phase(kind)
    if not key:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.TEMPLATE_VALUE,
            phase=phase,
            node_id=node_id,
            message=f"{kind} template key is empty.",
        )
        return None
    if kind == "env":
        environment = os.environ if options.environment is None else options.environment
        value = environment.get(key)
        label = "Environment variable"
    else:
        value = variables.get(key)
        label = "Template variable"
    if value is not None:
        return value
    append_diagnostic(
        state,
        code=PreflightDiagnosticCode.TEMPLATE_VALUE,
        phase=phase,
        node_id=node_id,
        message=f"{label} not set: {key}",
    )
    return None


def static_value_phase(kind: str) -> PreflightDiagnosticPhase:
    if kind == "env":
        return PreflightDiagnosticPhase.ENV_POLICY
    if kind == "var":
        return PreflightDiagnosticPhase.VAR_POLICY
    raise ValueError(f"Unsupported static value token kind: {kind}")
