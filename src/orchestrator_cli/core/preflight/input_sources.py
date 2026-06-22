from __future__ import annotations

from orchestrator_cli.core.workflow_models import WorkflowNode, WorkflowPlan

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    ResolvedStaticFileReference,
    allowed_template_paths,
    append_diagnostic,
    extend_diagnostics,
    node_source_span,
    source_root,
)
from .diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase
from .references import TemplateReference, iter_template_references
from .signatures import signature_for_payload
from .static_resources import append_static_resource, resolve_static_file
from .token_catalog import append_token_catalog
from .workspace.files.locators import (
    resolve_workspace_file_reference,
    token_signature_for_workspace_locator,
    workspace_locator_metadata,
    workspace_locator_resolved_payload,
)
from .workspace.files.selection import (
    is_allowlisted_absolute_path,
    workflow_has_managed_workspace_selection,
)


def resolve_input_source(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    source = node.source or ""
    references = iter_template_references(source)
    if len(references) != 1 or references[0].kind != "file":
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.INPUT_SOURCE,
            phase=PreflightDiagnosticPhase.FILE_POLICY,
            node_id=node.id,
            message=f"Input node '{node.id}' source must be one file token.",
        )
        return
    reference = references[0]
    occurrence_id = f"{node.id}:input:0"
    if should_use_input_workspace_file_locator(workflow, reference, options):
        resolve_workspace_input_source(
            workflow,
            node,
            reference,
            occurrence_id,
            options,
            state,
        )
        return
    resolve_static_input_source(node, reference, occurrence_id, options, state)


def should_use_input_workspace_file_locator(
    workflow: WorkflowPlan,
    reference: TemplateReference,
    options: PreflightCompileOptions,
) -> bool:
    return (
        options.workspace_source_snapshot is not None
        and workflow_has_managed_workspace_selection(workflow)
        and reference.key is not None
        and not is_allowlisted_absolute_path(
            reference.key,
            allowed_template_paths(options),
        )
    )


def resolve_workspace_input_source(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    reference: TemplateReference,
    occurrence_id: str,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    resolve_workspace_file_reference(
        workflow,
        node,
        "input_output",
        None,
        reference,
        occurrence_id,
        options,
        state,
    )
    locator = state.workspace_file_references.get(occurrence_id)
    if locator is None:
        return
    state.input_workspace_file_locator_ids[node.id] = locator.locator_id
    state.input_source_tokens[node.id] = reference


def resolve_static_input_source(
    node: WorkflowNode,
    reference: TemplateReference,
    occurrence_id: str,
    options: PreflightCompileOptions,
    state: CompileState,
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
    token_signature = signature_for_payload(
        {
            "content_ref": result.resource.content_ref,
            "node_id": node.id,
            "occurrence_id": occurrence_id,
            "raw_token": reference.raw_token,
            "sha256": result.resource.sha256,
            "size_bytes": result.resource.size_bytes,
        }
    )
    resource = result.resource.model_copy(
        update={"token_signatures": [token_signature]}
    )
    append_static_resource(state, resource, result.payload, token_signature)
    state.input_content_refs[node.id] = resource.content_ref
    state.input_source_tokens[node.id] = reference
    state.static_file_references[occurrence_id] = ResolvedStaticFileReference(
        resource=resource,
        token_signature=token_signature,
    )


def append_input_source_token_catalog(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    reference = state.input_source_tokens.get(node.id)
    if reference is None:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.TEMPLATE_PLAN,
            phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
            node_id=node.id,
            message=f"Input node '{node.id}' source was not resolved.",
        )
        return
    occurrence_id = f"{node.id}:input:0"
    workspace_locator = state.workspace_file_references.get(occurrence_id)
    if workspace_locator is not None:
        signature = token_signature_for_workspace_locator(workspace_locator)
        append_token_catalog(
            state=state,
            occurrence_id=occurrence_id,
            node=node,
            target_role="executor",
            source_role="shared",
            reference=reference,
            token_kind="file",
            fragment_index=0,
            signature=signature,
            options=options,
            source_span=node_source_span(node, options),
            canonical_locator=workspace_locator.locator_id,
            resolved=workspace_locator_resolved_payload(workspace_locator),
            metadata=workspace_locator_metadata(workspace_locator),
        )
        return
    resolution = state.static_file_references.get(occurrence_id)
    if resolution is None:
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.TEMPLATE_PLAN,
            phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
            node_id=node.id,
            message=f"Input node '{node.id}' source was not resolved.",
        )
        return
    resource = resolution.resource
    append_token_catalog(
        state=state,
        occurrence_id=occurrence_id,
        node=node,
        target_role="executor",
        source_role="shared",
        reference=reference,
        token_kind="file",
        fragment_index=0,
        signature=resolution.token_signature,
        options=options,
        source_span=node_source_span(node, options),
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
