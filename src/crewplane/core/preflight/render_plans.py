from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from crewplane.core.prompt_segments import PromptSegment, PromptSegmentRole
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan

from .compile_state import (
    CompileState,
    PreflightCompileOptions,
    allowed_template_paths,
    append_diagnostic,
    module_id,
    node_source_span,
    source_file,
    source_root,
)
from .diagnostics import PreflightDiagnosticCode, PreflightDiagnosticPhase
from .fragment_handlers import (
    file_reference_fragment,
    node_reference_fragment,
    resolve_file_reference,
    resolve_static_value_reference,
    static_value_fragment,
)
from .input_sources import append_input_source_token_catalog, resolve_input_source
from .models import Fragment, RenderPlan, RenderStream, WorkspaceFileTarget
from .plan_signatures import template_hash
from .references import TemplateReference, iter_template_references
from .signatures import signature_for_payload
from .workspace.files.locators import resolve_workspace_file_reference
from .workspace.files.selection import (
    is_allowlisted_absolute_path,
    node_selects_managed_workspace,
)


class RenderTargetRole(StrEnum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"


def apply_file_policy(
    workflow: WorkflowPlan,
    options: PreflightCompileOptions,
    state: CompileState,
) -> None:
    for node in workflow.nodes:
        if node.mode == "input":
            resolve_input_source(workflow, node, options, state)
    for occurrence in iter_render_token_occurrences(workflow):
        if occurrence.reference.kind != "file":
            continue
        if should_use_workspace_file_locator(
            workflow,
            occurrence.node,
            occurrence.reference,
            options,
        ):
            resolve_workspace_file_reference(
                workflow,
                occurrence.node,
                workspace_file_target_for_role(occurrence.target_role),
                occurrence.segment_index,
                occurrence.reference,
                occurrence.occurrence_id,
                options,
                state,
            )
            continue
        resolve_file_reference(
            occurrence.node,
            occurrence.reference,
            options,
            state,
            occurrence.occurrence_id,
        )


def should_use_workspace_file_locator(
    workflow: WorkflowPlan,
    node: WorkflowNode,
    reference: TemplateReference,
    options: PreflightCompileOptions,
) -> bool:
    return (
        options.workspace_source_snapshot is not None
        and node_selects_managed_workspace(workflow, node)
        and reference.key is not None
        and not is_allowlisted_absolute_reference(reference.key, options)
    )


@dataclass(frozen=True)
class RenderTokenOccurrence:
    node: WorkflowNode
    reference: TemplateReference
    occurrence_id: str
    target_role: RenderTargetRole
    source_role: PromptSegmentRole
    segment_index: int


def iter_render_token_occurrences(
    workflow: WorkflowPlan,
) -> Iterator[RenderTokenOccurrence]:
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        yield from iter_node_render_token_occurrences(node)


def iter_node_render_token_occurrences(
    node: WorkflowNode,
) -> Iterator[RenderTokenOccurrence]:
    for segment_index, segment in enumerate(node.prompt_segments):
        yield from iter_segment_render_token_occurrences(
            node,
            segment_index,
            segment,
        )


def iter_segment_render_token_occurrences(
    node: WorkflowNode,
    segment_index: int,
    segment: PromptSegment,
) -> Iterator[RenderTokenOccurrence]:
    target_roles = target_roles_for_segment(segment)
    for reference_index, reference in enumerate(
        iter_template_references(segment.content)
    ):
        for target_role in target_roles:
            yield RenderTokenOccurrence(
                node=node,
                reference=reference,
                occurrence_id=render_token_occurrence_id(
                    node.id,
                    target_role,
                    segment_index,
                    reference_index,
                ),
                target_role=target_role,
                source_role=segment.role,
                segment_index=segment_index,
            )


def target_roles_for_segment(
    segment: PromptSegment,
) -> tuple[RenderTargetRole, ...]:
    match segment.role:
        case "shared":
            return (RenderTargetRole.EXECUTOR, RenderTargetRole.REVIEWER)
        case "executor":
            return (RenderTargetRole.EXECUTOR,)
        case "reviewer":
            return (RenderTargetRole.REVIEWER,)
    raise ValueError(f"Unsupported prompt segment role: {segment.role!r}")


def workspace_file_target_for_role(
    target_role: RenderTargetRole,
) -> WorkspaceFileTarget:
    match target_role:
        case RenderTargetRole.EXECUTOR:
            return "executor_prompt"
        case RenderTargetRole.REVIEWER:
            return "reviewer_prompt"
    raise ValueError(f"Unsupported render target role: {target_role!r}")


def render_token_occurrence_id(
    node_id: str,
    target_role: RenderTargetRole,
    segment_index: int,
    reference_index: int,
) -> str:
    return f"{node_id}:{target_role.value}:{segment_index}:{reference_index}"


def is_allowlisted_absolute_reference(
    raw_path: str,
    options: PreflightCompileOptions,
) -> bool:
    return is_allowlisted_absolute_path(raw_path, allowed_template_paths(options))


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
            compile_render_stream(node, RenderTargetRole.EXECUTOR, options, state),
            compile_render_stream(node, RenderTargetRole.REVIEWER, options, state),
        ],
    )


def compile_render_stream(
    node: WorkflowNode,
    target_role: RenderTargetRole,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RenderStream:
    fragments: list[Fragment] = []
    for segment_index, segment in enumerate(node.prompt_segments):
        if target_role not in target_roles_for_segment(segment):
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
    return RenderStream(target_role=target_role.value, fragments=fragments)


def append_segment_fragments(
    node: WorkflowNode,
    target_role: RenderTargetRole,
    segment_index: int,
    segment: PromptSegment,
    options: PreflightCompileOptions,
    state: CompileState,
    fragments: list[Fragment],
) -> None:
    cursor = 0
    for occurrence in iter_segment_render_token_occurrences(
        node,
        segment_index,
        segment,
    ):
        if occurrence.target_role != target_role:
            continue
        reference = occurrence.reference
        if reference.start > cursor:
            fragments.append(
                Fragment(
                    fragment_index=len(fragments),
                    kind="literal",
                    source_role=segment.role,
                    text=segment.content[cursor : reference.start],
                )
            )
        fragment = fragment_for_occurrence(
            occurrence=occurrence,
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


def fragment_for_occurrence(
    occurrence: RenderTokenOccurrence,
    options: PreflightCompileOptions,
    state: CompileState,
    fragment_index: int,
) -> Fragment | None:
    node = occurrence.node
    target_role = occurrence.target_role.value
    reference = occurrence.reference
    if reference.kind == "node":
        return node_reference_fragment(
            node,
            target_role,
            occurrence.source_role,
            occurrence.segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence.occurrence_id,
        )
    if reference.kind == "file":
        return file_reference_fragment(
            node,
            target_role,
            occurrence.source_role,
            occurrence.segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence.occurrence_id,
        )
    if reference.kind in {"env", "var"}:
        return static_value_fragment(
            node,
            target_role,
            occurrence.source_role,
            occurrence.segment_index,
            reference,
            options,
            state,
            fragment_index,
            occurrence.occurrence_id,
        )
    if reference.kind == "param":
        append_diagnostic(
            state,
            code=PreflightDiagnosticCode.PARAM_TOKEN,
            phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
            node_id=node.id,
            message=f"Composition-only token '{reference.raw_token}' reached preflight.",
        )
        return None
    append_diagnostic(
        state,
        code=PreflightDiagnosticCode.TEMPLATE_TOKEN,
        phase=PreflightDiagnosticPhase.TEMPLATE_PLAN,
        node_id=node.id,
        message=f"Unsupported template token '{reference.raw_token}'.",
    )
    return None
