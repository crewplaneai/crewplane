from __future__ import annotations

import re
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegment
from crewplane.core.workflow.syntax import (
    NODE_ARTIFACT_REFERENCE_PATTERN,
    PARAM_TEMPLATE_PATTERN,
)

from .models import ParamBinding


def qualify_id(prefix: str, node_id: str) -> str:
    if not prefix:
        return node_id
    return f"{prefix}.{node_id}"


def rewrite_artifact_references(
    prompt: str,
    namespace_prefix: str,
    bound_input_nodes: dict[str, str],
) -> str:
    def _replace(match: re.Match[str]) -> str:
        reference_id = match.group(1)
        artifact_name = match.group(2)
        resolved_id = resolve_dependency_id(
            reference_id,
            namespace_prefix=namespace_prefix,
            bound_input_nodes=bound_input_nodes,
        )
        return "{{" + resolved_id + "." + artifact_name + "}}"

    return NODE_ARTIFACT_REFERENCE_PATTERN.sub(_replace, prompt)


def resolve_dependency_id(
    dependency_id: str,
    namespace_prefix: str,
    bound_input_nodes: dict[str, str],
) -> str:
    bound_target = bound_input_nodes.get(dependency_id)
    if bound_target is not None:
        return bound_target
    return qualify_id(namespace_prefix, dependency_id)


def resolve_param_templates(
    prompt: str,
    params: dict[str, ParamBinding],
    source_path: Path,
    node_id: str,
) -> tuple[str, set[str]]:
    consumed: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if not key:
            raise ValueError(
                "Workflow import parameter template has an empty key in "
                f"'{source_path}' node '{node_id}'."
            )
        binding = params.get(key)
        if binding is None:
            return "{{var:" + key + "}}"
        consumed.add(binding.binding_id)
        return binding.value

    return PARAM_TEMPLATE_PATTERN.sub(_replace, prompt), consumed


def rewrite_prompt_segments(
    prompt_segments: list[PromptSegment],
    namespace_prefix: str,
    bound_input_nodes: dict[str, str],
    params: dict[str, ParamBinding],
    source_path: Path,
    node_id: str,
) -> tuple[list[PromptSegment], set[str]]:
    rewritten_segments: list[PromptSegment] = []
    consumed_params: set[str] = set()

    for segment in prompt_segments:
        rewritten_prompt = rewrite_artifact_references(
            segment.content,
            namespace_prefix=namespace_prefix,
            bound_input_nodes=bound_input_nodes,
        )
        resolved_prompt, consumed = resolve_param_templates(
            rewritten_prompt,
            params=params,
            source_path=source_path,
            node_id=node_id,
        )
        consumed_params.update(consumed)
        rewritten_segments.append(
            PromptSegment(role=segment.role, content=resolved_prompt)
        )

    return rewritten_segments, consumed_params
