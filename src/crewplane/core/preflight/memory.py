"""Select role-specific historical context from already captured compilation data."""

from __future__ import annotations

from typing import Literal

from crewplane.core.config import MemorySettings
from crewplane.core.memory.query import (
    MAX_QUERY_CHARS,
    QueryContribution,
    QueryExclusion,
    build_query,
)
from crewplane.core.memory.retrieval import select_memory
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import WorkflowNode

from .compile_state import CompileState, PreflightCompileOptions
from .compiled_records import Fragment, RenderPlan
from .references import iter_template_references
from .render_plans import (
    RenderTokenOccurrence,
    iter_node_render_token_occurrences,
    target_roles_for_segment,
)


def append_memory_context(
    node: WorkflowNode,
    render_plan: RenderPlan,
    policy: MemorySettings,
    options: PreflightCompileOptions,
    state: CompileState,
) -> RenderPlan:
    if not node.memory:
        return render_plan
    if options.memory_snapshot is None:
        raise ValueError("Opted-in compilation requires a supplied memory snapshot.")
    streams = []
    roles = {provider.role for provider in node.providers}
    for stream in render_plan.streams:
        if stream.target_role not in roles:
            streams.append(stream)
            continue
        contributions, exclusions = _query_inputs(
            node, stream.target_role, options, state
        )
        selection = select_memory(
            options.memory_snapshot,
            node.id,
            stream.target_role.value,
            build_query(tuple(contributions), tuple(exclusions)),
            policy.max_records,
            policy.max_chars,
            len(stream.fragments),
        )
        state.memory_selections.append(selection)
        fragments = list(stream.fragments)
        if selection.rendered:
            fragments.append(
                Fragment(
                    fragment_index=len(fragments),
                    kind="literal",
                    source_role=PromptSegmentRole.SHARED,
                    text=selection.rendered,
                )
            )
        streams.append(stream.model_copy(update={"fragments": fragments}))
    return render_plan.model_copy(update={"streams": streams})


def _query_inputs(
    node: WorkflowNode,
    role: ProviderRole,
    options: PreflightCompileOptions,
    state: CompileState,
) -> tuple[list[QueryContribution], list[QueryExclusion]]:
    contributions = [QueryContribution(kind="identifier", source="node", text=node.id)]
    source = options.node_source_files.get(node.id)
    if source is not None:
        source_path = (
            source.relative_to(options.project_root)
            if source.is_relative_to(options.project_root)
            else source
        )
        contributions.append(
            QueryContribution(
                kind="identifier", source="workflow", text=source_path.as_posix()
            )
        )
    for index, segment in enumerate(node.prompt_segments):
        if role not in target_roles_for_segment(segment):
            continue
        references = iter_template_references(segment.content)
        cursor = 0
        literals = []
        for reference in references:
            literals.append(segment.content[cursor : reference.start])
            cursor = reference.end
        literals.append(segment.content[cursor:])
        contributions.append(
            QueryContribution(
                kind="literal",
                source=f"{node.id}:{role.value}:{index}",
                text=" ".join(literals),
            )
        )
    exclusions = []
    for occurrence in iter_node_render_token_occurrences(node):
        if occurrence.target_role == role:
            contribution = _captured_contribution(occurrence, options, state)
            if isinstance(contribution, QueryContribution):
                contributions.append(contribution)
            else:
                exclusions.append(contribution)
    return contributions, exclusions


def _captured_contribution(
    occurrence: RenderTokenOccurrence,
    options: PreflightCompileOptions,
    state: CompileState,
) -> QueryContribution | QueryExclusion:
    reference, identity = occurrence.reference, occurrence.occurrence_id
    if reference.kind == "env":
        return QueryExclusion(source=identity, reason="environment values are excluded")
    if reference.kind == "var":
        value = state.static_value_references[identity]
        if value.sensitive or value.value_handle is not None:
            return QueryExclusion(
                source=identity, reason="sensitive variable is excluded"
            )
        return QueryContribution(
            kind="variable", source=identity, text=value.value_stored or ""
        )
    if reference.kind == "file":
        return _file_contribution(identity, "file", state)
    if reference.kind == "node":
        input_id = reference.node_id or ""
        if reference.artifact_name != "output":
            return QueryExclusion(
                source=identity,
                reason="runtime artifact metadata or findings are excluded",
            )
        if input_id in options.item_input_ids:
            return QueryExclusion(
                source=identity,
                reason="runtime collection item is unavailable during compilation",
            )
        input_identity = f"{input_id}:input:0"
        if (
            input_id in state.input_content_refs
            or input_id in state.input_workspace_file_locator_ids
        ):
            contribution = _file_contribution(input_identity, "input", state)
            return contribution.model_copy(update={"source": identity})
        return QueryExclusion(
            source=identity, reason="provider-generated or unavailable runtime output"
        )
    return QueryExclusion(source=identity, reason="unsupported query contribution")


def _file_contribution(
    identity: str,
    kind: Literal["file", "input"],
    state: CompileState,
) -> QueryContribution | QueryExclusion:
    if identity in state.terminal_result_occurrences:
        return QueryExclusion(
            source=identity, reason="retained provider-generated output is excluded"
        )
    static = state.static_file_references.get(identity)
    if static is not None:
        payload = state.static_payloads[static.resource.content_ref]
    else:
        locator = state.workspace_file_references.get(identity)
        if locator is None or locator.content_ref is None:
            return QueryExclusion(
                source=identity, reason="runtime workspace file bytes are unavailable"
            )
        payload = state.workspace_file_payloads[locator.content_ref]
    return QueryContribution(
        kind=kind,
        source=identity,
        text=_bounded_captured_text(payload),
    )


def _bounded_captured_text(payload: bytes) -> str:
    # The extra character preserves the query builder's explicit omission receipt.
    return payload[: 4 * (MAX_QUERY_CHARS + 1)].decode("utf-8", errors="ignore")[
        : MAX_QUERY_CHARS + 1
    ]
