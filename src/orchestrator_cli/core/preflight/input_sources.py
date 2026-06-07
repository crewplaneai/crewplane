from __future__ import annotations

from orchestrator_cli.core.workflow_models import WorkflowNode

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
from .references import iter_template_references
from .signatures import signature_for_payload
from .static_resources import append_static_resource, resolve_static_file
from .token_catalog import append_token_catalog


def resolve_input_source(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    source = node.source or ""
    references = iter_template_references(source)
    if len(references) != 1 or references[0].kind != "file":
        append_diagnostic(
            state,
            code="INPUT-SOURCE",
            phase="file_policy",
            node_id=node.id,
            message=f"Input node '{node.id}' source must be one file token.",
        )
        return
    reference = references[0]
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
            "occurrence_id": f"{node.id}:input:0",
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
    state.static_file_references[f"{node.id}:input:0"] = ResolvedStaticFileReference(
        resource=resource,
        token_signature=token_signature,
    )


def append_input_source_token_catalog(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    reference = state.input_source_tokens.get(node.id)
    resolution = state.static_file_references.get(f"{node.id}:input:0")
    if reference is None or resolution is None:
        append_diagnostic(
            state,
            code="TEMPLATE-PLAN",
            phase="template_plan",
            node_id=node.id,
            message=f"Input node '{node.id}' source was not resolved.",
        )
        return
    resource = resolution.resource
    append_token_catalog(
        state=state,
        occurrence_id=f"{node.id}:input:0",
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
