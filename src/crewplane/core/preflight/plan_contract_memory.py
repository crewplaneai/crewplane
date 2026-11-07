"""Validate frozen recall without consulting today's mutable memory store."""

from __future__ import annotations

from crewplane.core.memory.retrieval import MemorySelection

from .compiled_records import PreflightExecutionNode, RenderPlan, RenderStream


def validate_memory_contract(
    nodes: list[PreflightExecutionNode],
    render_plans: list[RenderPlan],
    selections: list[MemorySelection],
) -> None:
    """Validate frozen recall coverage and role-stream append constraints.

    Require exactly one selection per memory-enabled scheduled node role. After
    coverage passes, revalidate each serialized selection in supplied order,
    then check its owned stream and frozen append boundary. Nonempty recall
    must be the complete final shared literal; empty recall adds no fragment.
    Return None without mutating inputs or consulting the mutable memory store.

    Raises:
        ValueError: Coverage, stream ownership, or append constraints fail at
            the first failed check.
        pydantic.ValidationError: A serialized frozen selection is invalid.
    """
    _validate_role_coverage(nodes, selections)
    renders = {render.node_id: render for render in render_plans}
    for selection in selections:
        validated = MemorySelection.model_validate(selection.model_dump(mode="json"))
        stream = _owned_role_stream(renders.get(selection.node_id), selection)
        _validate_memory_append(stream, validated)


def _validate_role_coverage(
    nodes: list[PreflightExecutionNode], selections: list[MemorySelection]
) -> None:
    expected = {
        (node.id, provider.role.value)
        for node in nodes
        if node.memory
        for provider in node.provider_records
    }
    keys = [(selection.node_id, selection.role) for selection in selections]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError(
            "Memory selections must match every enabled scheduled role exactly once."
        )


def _owned_role_stream(
    render: RenderPlan | None, selection: MemorySelection
) -> RenderStream:
    streams = (
        []
        if render is None
        else [
            stream for stream in render.streams if stream.target_role == selection.role
        ]
    )
    if len(streams) != 1:
        raise ValueError("Memory selection requires exactly one owned role stream.")
    return streams[0]


def _validate_memory_append(stream: RenderStream, selection: MemorySelection) -> None:
    fragments = stream.fragments
    if len(fragments) != selection.append_after_fragment_count + bool(
        selection.rendered
    ):
        raise ValueError(
            "Memory rendering disagrees with its frozen role-stream append boundary."
        )
    if not selection.rendered:
        return
    if not fragments or (
        fragments[-1].kind != "literal"
        or fragments[-1].source_role != "shared"
        or fragments[-1].text != selection.rendered
        or fragments[-1].fragment_index != len(fragments) - 1
    ):
        raise ValueError(
            "Memory rendering must be the complete final shared literal of its owned role stream."
        )
