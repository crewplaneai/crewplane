from __future__ import annotations

from pathlib import Path

from crewplane.core.prompt_segments import PromptSegmentPayload
from crewplane.core.workspace.policy import worktree_declarations_payload

from ..models import (
    WorkflowNodePayload,
    WorkflowPayload,
    workflow_node_payload_dict,
)
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
    WorkflowValidationSummary,
)


def build_workflow_payload(
    workflow: WorkflowFrontmatter,
    parsed_body: ParsedWorkflowBody,
    source: Path,
) -> tuple[WorkflowPayload, dict[str, dict[str, int]], dict[str, list[dict[str, int]]]]:
    node_payload: list[WorkflowNodePayload] = []
    node_source_spans: dict[str, dict[str, int]] = {}
    prompt_segment_spans_by_node: dict[str, list[dict[str, int]]] = {}
    for node in workflow.nodes:
        section_prompts = parsed_body.node_sections.get(node.id, [])
        section_text = section_prompts[0] if section_prompts else ""
        section_content_starts = parsed_body.node_section_content_start_lines.get(
            node.id,
            [],
        )
        content_start_line = section_content_starts[0] if section_content_starts else 0
        section_spans = parsed_body.node_section_spans.get(node.id, [])
        if section_spans:
            node_source_spans[node.id] = section_spans[0]
        providers = [
            normalize_provider_spec(provider, source) for provider in node.providers
        ]

        if node.mode == "input":
            validate_input_node_section(section_text, source, node.id)
            prompt_segments: list[PromptSegmentPayload] = []
            prompt_segment_spans: list[dict[str, int]] = []
        else:
            prompt_segments, prompt_segment_spans = extract_prompt_segment_payloads(
                section_text,
                source=source,
                node_id=node.id,
                content_start_line=content_start_line,
            )
        prompt_segment_spans_by_node[node.id] = prompt_segment_spans

        workflow_node = workflow_node_from_frontmatter(
            node,
            providers,
            prompt_segments,
            source,
        )
        node_payload.append(workflow_node_payload_dict(workflow_node))

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
            }
            for import_config in workflow.imports
        ],
        "nodes": node_payload,
    }
    if workflow.worktrees:
        payload["worktrees"] = worktree_declarations_payload(workflow.worktrees)
    return payload, node_source_spans, prompt_segment_spans_by_node


def build_validation_summary(
    workflow: WorkflowFrontmatter,
    parsed_body: ParsedWorkflowBody,
) -> WorkflowValidationSummary:
    edge_count = sum(len(node.needs) for node in workflow.nodes)
    return WorkflowValidationSummary(
        nodes_defined=len(workflow.nodes),
        node_sections_found=len(parsed_body.section_headers),
        edges_defined=edge_count,
    )
