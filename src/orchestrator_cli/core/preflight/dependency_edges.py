from __future__ import annotations

from .compile_state import CompileState
from .models import DependencyEdge
from .signatures import signature_for_payload


def dependency_signature(
    source_node: str,
    target_node: str,
    artifact_name: str | None,
) -> str:
    return signature_for_payload(
        {
            "artifact_name": artifact_name,
            "source": source_node,
            "target": target_node,
        }
    )


def append_dependency_edge(
    state: CompileState,
    source_node: str,
    target_node: str,
    artifact_name: str | None,
    first_token_signature: str | None = None,
) -> None:
    key = (source_node, target_node, artifact_name)
    if key in state.dependency_edges:
        if first_token_signature is None:
            return
        existing = state.dependency_edges[key]
        if existing.first_token_signature is None:
            state.dependency_edges[key] = existing.model_copy(
                update={"first_token_signature": first_token_signature}
            )
        return
    edge_signature = dependency_signature(
        source_node=source_node,
        target_node=target_node,
        artifact_name=artifact_name,
    )
    state.dependency_edges[key] = DependencyEdge(
        source_node=source_node,
        target_node=target_node,
        artifact_name=artifact_name,
        artifact_key=artifact_name,
        target_locator=(
            f"{source_node}.{artifact_name}"
            if artifact_name is not None
            else source_node
        ),
        first_token_signature=first_token_signature,
        dependency_signature=edge_signature,
    )
