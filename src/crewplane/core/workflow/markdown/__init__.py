from __future__ import annotations

from pathlib import Path

from yaml.constructor import ConstructorError

from crewplane.core.yaml_loader import load_yaml_unique

from ..models import WorkflowPayload
from .frontmatter import read_workflow_text, split_frontmatter
from .models import (
    ParsedWorkflowMarkdown,
    WorkflowFrontmatter,
    WorkflowValidationSummary,
)
from .payloads import build_validation_summary, build_workflow_payload
from .sections import parse_workflow_body, validate_node_sections


def validate_workflow_markdown(path: Path) -> WorkflowValidationSummary:
    """Validate a workflow Markdown file and summarize structural issues."""

    markdown_text = read_workflow_text(path)
    return validate_workflow_markdown_text(path, markdown_text)


def validate_workflow_markdown_text(
    path: Path,
    markdown_text: str,
) -> WorkflowValidationSummary:
    """Validate workflow Markdown content and summarize structural issues."""

    parsed = parse_workflow_markdown_document(path, markdown_text)
    return build_validation_summary(parsed.frontmatter, parsed.parsed_body)


def parse_workflow_markdown(path: Path) -> WorkflowPayload:
    """Parse a workflow Markdown file into a workflow payload."""

    markdown_text = read_workflow_text(path)
    return parse_workflow_markdown_text(path, markdown_text)


def parse_workflow_markdown_text(path: Path, markdown_text: str) -> WorkflowPayload:
    """Parse workflow Markdown content into a workflow payload."""

    return parse_workflow_markdown_document(path, markdown_text).payload


def parse_workflow_markdown_document(
    path: Path,
    markdown_text: str,
) -> ParsedWorkflowMarkdown:
    """Parse workflow Markdown content into a structured document."""

    frontmatter_text, body, body_start_line = split_frontmatter(markdown_text, path)
    try:
        frontmatter = load_yaml_unique(frontmatter_text)
    except ConstructorError as error:
        raise ValueError(f"{path} frontmatter is invalid: {error}") from error
    if not isinstance(frontmatter, dict):
        raise ValueError(f"{path} frontmatter must be a YAML object.")

    workflow = WorkflowFrontmatter(**frontmatter)
    declared_node_ids = {node.id for node in workflow.nodes}
    parsed_body = parse_workflow_body(body, declared_node_ids, body_start_line)
    validate_node_sections(workflow, parsed_body, path)
    payload, node_source_spans, prompt_segment_spans = build_workflow_payload(
        workflow,
        parsed_body,
        path,
    )
    return ParsedWorkflowMarkdown(
        frontmatter=workflow,
        parsed_body=parsed_body,
        payload=payload,
        node_source_spans=node_source_spans,
        prompt_segment_spans=prompt_segment_spans,
    )
