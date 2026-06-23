from __future__ import annotations

from pathlib import Path

from markdown_it import MarkdownIt

from .models import (
    MarkdownSection,
    ParsedWorkflowBody,
    WorkflowFrontmatter,
)

COMMONMARK_PARSER = MarkdownIt("commonmark")


def collect_document_root_sections(
    body: str,
    declared_node_ids: set[str],
) -> list[MarkdownSection]:
    tokens = COMMONMARK_PARSER.parse(body)
    headings: list[tuple[str, int, int]] = []

    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag != "h2" or token.level != 0:
            continue
        if token.map is None:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].type != "inline":
            continue
        header_text = tokens[index + 1].content.strip()
        headings.append((header_text, token.map[0], token.map[1]))

    section_ranges: list[MarkdownSection] = []
    body_line_count = len(body.splitlines(keepends=True))
    declared_headings = [
        heading for heading in headings if heading[0] in declared_node_ids
    ]
    for index, heading in enumerate(declared_headings):
        header = heading[0]
        content_start = heading[2]
        next_heading_start = (
            declared_headings[index + 1][1]
            if index + 1 < len(declared_headings)
            else body_line_count
        )
        section_ranges.append(
            MarkdownSection(
                header=header,
                header_start_line=heading[1],
                content_start_line=content_start,
                content_end_line=next_heading_start,
            )
        )
    return section_ranges


def parse_workflow_body(
    body: str,
    declared_node_ids: set[str],
    body_start_line: int,
) -> ParsedWorkflowBody:
    lines = body.splitlines(keepends=True)
    sections = collect_document_root_sections(body, declared_node_ids)

    node_sections: dict[str, list[str]] = {}
    node_section_content_start_lines: dict[str, list[int]] = {}
    node_section_spans: dict[str, list[dict[str, int]]] = {}
    section_headers: list[str] = []
    for section in sections:
        section_headers.append(section.header)
        section_text = "".join(
            lines[section.content_start_line : section.content_end_line]
        )
        node_sections.setdefault(section.header, []).append(section_text)
        node_section_content_start_lines.setdefault(section.header, []).append(
            body_start_line + section.content_start_line
        )
        node_section_spans.setdefault(section.header, []).append(
            {
                "start_line": body_start_line + section.header_start_line,
                "end_line": body_start_line + section.content_end_line,
            }
        )

    return ParsedWorkflowBody(
        node_sections=node_sections,
        node_section_content_start_lines=node_section_content_start_lines,
        node_section_spans=node_section_spans,
        section_headers=section_headers,
    )


def validate_node_sections(
    workflow: WorkflowFrontmatter,
    parsed_body: ParsedWorkflowBody,
    source: Path,
) -> None:
    errors: list[str] = []
    nodes_by_id = {node.id: node for node in workflow.nodes}
    declared_node_ids = set(nodes_by_id)

    for node_id in sorted(declared_node_ids):
        section_prompts = parsed_body.node_sections.get(node_id)
        if section_prompts is None:
            if nodes_by_id[node_id].mode == "input":
                continue
            errors.append(f"Missing node section '## {node_id}' in markdown body.")
            continue
        if len(section_prompts) > 1:
            errors.append(
                f"Duplicate node section '## {node_id}' found {len(section_prompts)} times."
            )

    input_node_ids = {node.id for node in workflow.nodes if node.mode == "input"}
    for input_node_id in sorted(input_node_ids):
        section_count = len(parsed_body.node_sections.get(input_node_id, []))
        if section_count == 0:
            continue
        errors.append(
            f"Input node '{input_node_id}' must not define a markdown section."
        )

    if errors:
        raise ValueError(f"{source} is invalid:\n" + "\n".join(errors))
