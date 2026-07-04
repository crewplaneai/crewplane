from __future__ import annotations

from collections.abc import Mapping

from .rendering_errors import OnboardingRenderingError
from .rendering_providers import validate_known_provider
from .rendering_yaml_blocks import (
    frontmatter_mapping_key_line_index,
    join_lines_like,
    leading_spaces,
    line_indent,
    yaml_child_block_end,
)
from .rendering_yaml_loading import load_yaml_mapping


def render_provider_ready_workflow(default_workflow: str, provider: str) -> str:
    validate_known_provider(provider)
    lines, provider_line_index = workflow_provider_line(default_workflow, "mock")
    indent = line_indent(lines[provider_line_index])
    lines[provider_line_index] = f"{indent}providers: [{provider}]"
    text = join_lines_like(default_workflow, lines)
    validate_provider_ready_workflow(text, provider)
    return text


def manual_workflow_snippet(default_workflow: str, provider: str) -> str:
    validate_known_provider(provider)
    provider_ready_workflow = render_provider_ready_workflow(
        default_workflow,
        provider,
    )
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        provider_ready_workflow
    )
    nodes_index = frontmatter_mapping_key_line_index(
        lines,
        frontmatter_start,
        frontmatter_end,
        "nodes",
        0,
        "default workflow nodes",
    )
    nodes_end = yaml_child_block_end(
        lines, nodes_index, leading_spaces(lines[nodes_index])
    )
    return "\n".join(lines[nodes_index:nodes_end])


def validate_provider_ready_workflow(workflow_text: str, provider: str) -> None:
    providers = workflow_frontmatter_providers(workflow_text)
    if providers != [provider]:
        raise OnboardingRenderingError(
            "Provider-ready workflow must reference only the selected provider."
        )


def workflow_provider_line(
    workflow_text: str,
    expected_provider: str,
) -> tuple[list[str], int]:
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        workflow_text
    )
    providers = workflow_frontmatter_providers_from_lines(
        lines,
        frontmatter_start,
        frontmatter_end,
    )
    if providers != [expected_provider]:
        raise OnboardingRenderingError(
            "Default workflow provider list is not the generated mock setup."
        )
    provider_line_indices = [
        index
        for index in range(frontmatter_start, frontmatter_end)
        if lines[index].lstrip().startswith("providers:")
    ]
    if len(provider_line_indices) != 1:
        raise OnboardingRenderingError(
            "Default workflow must contain one providers line."
        )
    return lines, provider_line_indices[0]


def workflow_frontmatter_providers(workflow_text: str) -> list[str]:
    lines, frontmatter_start, frontmatter_end = workflow_frontmatter_bounds(
        workflow_text
    )
    return workflow_frontmatter_providers_from_lines(
        lines,
        frontmatter_start,
        frontmatter_end,
    )


def workflow_frontmatter_providers_from_lines(
    lines: list[str],
    frontmatter_start: int,
    frontmatter_end: int,
) -> list[str]:
    data = load_yaml_mapping(
        "\n".join(lines[frontmatter_start:frontmatter_end]),
        "default workflow frontmatter",
    )
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 1:
        raise OnboardingRenderingError(
            "Default workflow must contain one frontmatter node."
        )
    node = nodes[0]
    if not isinstance(node, Mapping):
        raise OnboardingRenderingError("Default workflow node must be a mapping.")
    providers = node.get("providers")
    if not isinstance(providers, list) or not all(
        isinstance(provider, str) for provider in providers
    ):
        raise OnboardingRenderingError(
            "Default workflow node must contain provider names."
        )
    return providers


def workflow_frontmatter_bounds(workflow_text: str) -> tuple[list[str], int, int]:
    lines = workflow_text.splitlines()
    if not lines or lines[0] != "---":
        raise OnboardingRenderingError("Default workflow is missing frontmatter.")
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError as error:
        raise OnboardingRenderingError(
            "Default workflow frontmatter is not closed."
        ) from error
    return lines, 1, frontmatter_end


__all__ = [
    "manual_workflow_snippet",
    "render_provider_ready_workflow",
]
