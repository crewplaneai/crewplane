"""Validate frozen recall without consulting today's mutable memory store."""

from __future__ import annotations

from crewplane.core.memory.retrieval import MemorySelection

from .compiled_records import PreflightExecutionNode, RenderPlan


def validate_memory_contract(
    nodes: list[PreflightExecutionNode],
    render_plans: list[RenderPlan],
    selections: list[MemorySelection],
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
    renders = {render.node_id: render for render in render_plans}
    for selection in selections:
        validated = MemorySelection.model_validate(selection.model_dump(mode="json"))
        render = renders.get(selection.node_id)
        streams = (
            []
            if render is None
            else [
                stream
                for stream in render.streams
                if stream.target_role == selection.role
            ]
        )
        if len(streams) != 1:
            raise ValueError("Memory selection requires exactly one owned role stream.")
        fragments = streams[0].fragments
        if len(fragments) != validated.append_after_fragment_count + bool(
            validated.rendered
        ):
            raise ValueError(
                "Memory rendering disagrees with its frozen role-stream append boundary."
            )
        if not validated.rendered:
            continue
        if not fragments or (
            fragments[-1].kind != "literal"
            or fragments[-1].source_role != "shared"
            or fragments[-1].text != validated.rendered
            or fragments[-1].fragment_index != len(fragments) - 1
        ):
            raise ValueError(
                "Memory rendering must be the complete final shared literal of its owned role stream."
            )
