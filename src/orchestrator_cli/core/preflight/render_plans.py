from __future__ import annotations

from dataclasses import dataclass

from orchestrator_cli.core.prompt_segments import PromptSegment, PromptSegmentRole
from orchestrator_cli.core.workflow_models import WorkflowNode, WorkflowPlan

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    append_diagnostic,
    module_id,
    node_source_span,
    source_file,
    source_root,
)
from .fragment_handlers import (
    file_reference_fragment,
    node_reference_fragment,
    resolve_file_reference,
    resolve_static_value_reference,
    static_value_fragment,
)
from .input_sources import append_input_source_token_catalog, resolve_input_source
from .models import Fragment, RenderPlan, RenderStream
from .plan_signatures import template_hash
from .references import TemplateReference, iter_template_references
from .signatures import signature_for_payload


@dataclass(frozen=True)
class RenderTokenOccurrence:
    node: WorkflowNode
    reference: TemplateReference
    occurrence_id: str


def apply_file_policy(
    workflow: WorkflowPlan,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    for node in workflow.nodes:
        if node.mode == "input":
            resolve_input_source(node, options, state)
    for occurrence in iter_render_token_occurrences(workflow):
        if occurrence.reference.kind != "file":
            continue
        resolve_file_reference(
            occurrence.node,
            occurrence.reference,
            options,
            state,
            occurrence.occurrence_id,
        )


def apply_env_policy(
    workflow: WorkflowPlan,
    variables: dict[str, str],
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    apply_static_value_policy(workflow, "env", variables, options, state)


def apply_var_policy(
    workflow: WorkflowPlan,
    variables: dict[str, str],
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    apply_static_value_policy(workflow, "var", variables, options, state)


def apply_static_value_policy(
    workflow: WorkflowPlan,
    token_kind: str,
    variables: dict[str, str],
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    for occurrence in iter_render_token_occurrences(workflow):
        if occurrence.reference.kind != token_kind:
            continue
        resolve_static_value_reference(
            occurrence.node,
            occurrence.reference,
            variables,
            options,
            state,
            occurrence.occurrence_id,
        )


def iter_render_token_occurrences(
    workflow: WorkflowPlan,
) -> tuple[RenderTokenOccurrence, ...]:
    occurrences: list[RenderTokenOccurrence] = []
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        for target_role in ("executor", "reviewer"):
            for segment in node.prompt_segments:
                if segment.role not in ("shared", target_role):
                    continue
                for reference in iter_template_references(segment.content):
                    occurrences.append(
                        RenderTokenOccurrence(
                            node=node,
                            reference=reference,
                            occurrence_id=(
                                f"{node.id}:{target_role}:{len(occurrences)}"
                            ),
                        )
                    )
    return tuple(occurrences)


def compile_render_plan(
    node: WorkflowNode,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RenderPlan | None:
    if node.mode == "input":
        append_input_source_token_catalog(node, options, state)
        return None
    compiled_template_hash = template_hash(node)
    return RenderPlan(
        render_plan_id=node.id,
        node_id=node.id,
        module_id=module_id(node, options),
        source_file=source_file(node, options),
        source_root=source_root(node, options).as_posix(),
        source_span=node_source_span(node, options),
        template_hash=compiled_template_hash,
        template_signature=signature_for_payload(
            {"node_id": node.id, "template_hash": compiled_template_hash}
        ),
        streams=[
            compile_render_stream(node, "executor", options, state),
            compile_render_stream(node, "reviewer", options, state),
        ],
    )


def compile_render_stream(
    node: WorkflowNode,
    target_role: str,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RenderStream:
    fragments: list[Fragment] = []
    for segment_index, segment in enumerate(node.prompt_segments):
        if segment.role not in ("shared", target_role):
            continue
        append_segment_fragments(
            node=node,
            target_role=target_role,
            segment_index=segment_index,
            segment=segment,
            options=options,
            state=state,
            fragments=fragments,
        )
    return RenderStream(target_role=target_role, fragments=fragments)


def append_segment_fragments(
    node: WorkflowNode,
    target_role: str,
    segment_index: int,
    segment: PromptSegment,
    options: PreflightCompileOptions,
    state: CompileState,
    fragments: list[Fragment],
) -> None:
    cursor = 0
    for reference in iter_template_references(segment.content):
        if reference.start > cursor:
            fragments.append(
                Fragment(
                    fragment_index=len(fragments),
                    kind="literal",
                    source_role=segment.role,
                    text=segment.content[cursor : reference.start],
                )
            )
        fragment = fragment_for_reference(
            node=node,
            target_role=target_role,
            source_role=segment.role,
            segment_index=segment_index,
            reference=reference,
            options=options,
            state=state,
            fragment_index=len(fragments),
        )
        if fragment is not None:
            fragments.append(fragment)
        cursor = reference.end
    if cursor < len(segment.content):
        fragments.append(
            Fragment(
                fragment_index=len(fragments),
                kind="literal",
                source_role=segment.role,
                text=segment.content[cursor:],
            )
        )


def fragment_for_reference(
    node: WorkflowNode,
    target_role: str,
    source_role: PromptSegmentRole,
    segment_index: int,
    reference: TemplateReference,
    options: PreflightCompileOptions,
    state: CompileState,
    fragment_index: int,
) -> Fragment | None:
    occurrence_id = f"{node.id}:{target_role}:{state.render_token_index}"
    state.render_token_index += 1
    if reference.kind == "node":
        return node_reference_fragment(
            node,
            target_role,
            source_role,
            segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence_id,
        )
    if reference.kind == "file":
        return file_reference_fragment(
            node,
            target_role,
            source_role,
            segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence_id,
        )
    if reference.kind in {"env", "var"}:
        return static_value_fragment(
            node,
            target_role,
            source_role,
            segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence_id,
        )
    if reference.kind == "param":
        append_diagnostic(
            state,
            code="PARAM-TOKEN",
            phase="template_plan",
            node_id=node.id,
            message=f"Composition-only token '{reference.raw_token}' reached preflight.",
        )
        return None
    append_diagnostic(
        state,
        code="TEMPLATE-TOKEN",
        phase="template_plan",
        node_id=node.id,
        message=f"Unsupported template token '{reference.raw_token}'.",
    )
    return None
