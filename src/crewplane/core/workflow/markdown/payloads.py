"""Assemble Markdown workflow payloads, source maps, and structural summaries."""

from __future__ import annotations

import re
from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentPayload
from crewplane.core.workspace.policy import worktree_declarations_payload

from ..models import (
    WorkflowNodePayload,
    WorkflowPayload,
    workflow_node_payload_dict,
)
from ..source_locations import SourceSpan
from .frontmatter import (
    normalize_provider_spec,
    workflow_node_from_frontmatter,
)
from .markers import (
    extract_prompt_segment_payloads,
    validate_input_node_section,
)
from .models import (
    ParsedWorkflowBody,
    WorkflowFrontmatter,
    WorkflowNodeConfig,
    WorkflowValidationSummary,
)


def build_workflow_payload(
    workflow: WorkflowFrontmatter,
    parsed_body: ParsedWorkflowBody,
    source: Path,
) -> tuple[WorkflowPayload, dict[str, SourceSpan], dict[str, list[SourceSpan]]]:
    """Build a payload and source maps after node sections have been validated.

    Callers must first run ``validate_node_sections`` on the frontmatter and body.
    Nodes and source-map entries retain frontmatter order.

    Args:
        workflow: Validated workflow frontmatter.
        parsed_body: Parsed Markdown body with validated section structure.
        source: Workflow path used in validation errors.

    Returns:
        A tuple containing the serialized workflow payload, section spans for
        nodes with sections, and prompt-segment spans for every node. Input and
        foreach nodes have empty prompt-span lists.

    Raises:
        ValueError: A provider name is empty, section content or role markers are
            invalid, or a node declaration fails validation.
    """
    node_payload: list[WorkflowNodePayload] = []
    node_source_spans: dict[str, SourceSpan] = {}
    prompt_segment_spans_by_node: dict[str, list[SourceSpan]] = {}
    for node in workflow.nodes:
        payload_node, node_span, prompt_spans = _build_node_payload(
            node, parsed_body, source
        )
        if node_span is not None:
            node_source_spans[node.id] = node_span
        prompt_segment_spans_by_node[node.id] = prompt_spans
        node_payload.append(payload_node)

    payload: WorkflowPayload = {
        "schema_version": workflow.schema_version,
        "name": workflow.name,
        "description": workflow.description or "",
        "inputs": dict(workflow.inputs),
        "imports": [
            {
                "path": import_config.path,
                "as": import_config.alias,
                "with": dict(import_config.with_params),
                "inputs": dict(import_config.input_bindings),
                "needs": list(import_config.needs),
            }
            for import_config in workflow.imports
        ],
        "nodes": node_payload,
    }
    if workflow.repeat_force_run_count is not None:
        payload["repeat_force_run_count"] = workflow.repeat_force_run_count
    if workflow.worktrees:
        payload["worktrees"] = worktree_declarations_payload(workflow.worktrees)
    return payload, node_source_spans, prompt_segment_spans_by_node


def _build_node_payload(
    node: WorkflowNodeConfig,
    parsed_body: ParsedWorkflowBody,
    source: Path,
) -> tuple[WorkflowNodePayload, SourceSpan | None, list[SourceSpan]]:
    section_prompts = parsed_body.node_sections.get(node.id, [])
    section_text = section_prompts[0] if section_prompts else ""
    section_content_starts = parsed_body.node_section_content_start_lines.get(
        node.id,
        [],
    )
    content_start_line = section_content_starts[0] if section_content_starts else 0
    section_spans = parsed_body.node_section_spans.get(node.id, [])
    node_span = section_spans[0] if section_spans else None
    providers = [
        normalize_provider_spec(provider, source) for provider in node.providers
    ]
    prompt_segments, prompt_spans = _build_node_prompt_segments(
        node, section_text, source, content_start_line
    )
    workflow_node = workflow_node_from_frontmatter(
        node,
        providers,
        prompt_segments,
        source,
    )
    return workflow_node_payload_dict(workflow_node), node_span, prompt_spans


def _build_node_prompt_segments(
    node: WorkflowNodeConfig,
    section_text: str,
    source: Path,
    content_start_line: int,
) -> tuple[list[PromptSegmentPayload], list[SourceSpan]]:
    if node.mode == "foreach":
        if re.sub(r"<!--.*?-->", "", section_text, flags=re.DOTALL).strip():
            raise ValueError(
                f"Foreach '{node.id}' section must be empty or comment-only"
            )
        return [], []
    if node.mode == "input":
        validate_input_node_section(section_text, source, node.id)
        return [], []
    return extract_prompt_segment_payloads(
        section_text,
        source=source,
        node_id=node.id,
        content_start_line=content_start_line,
    )


def build_validation_summary(
    workflow: WorkflowFrontmatter,
    parsed_body: ParsedWorkflowBody,
) -> WorkflowValidationSummary:
    """Count frontmatter nodes, parsed section headers, and dependency entries.

    Counts use the supplied frontmatter and body without validation or deduplication.
    """
    edge_count = sum(len(node.needs) for node in workflow.nodes)
    return WorkflowValidationSummary(
        nodes_defined=len(workflow.nodes),
        node_sections_found=len(parsed_body.section_headers),
        edges_defined=edge_count,
    )
