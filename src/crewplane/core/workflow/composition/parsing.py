from __future__ import annotations

from pathlib import Path

from ..markdown.models import ParsedWorkflowMarkdown
from ..models import WorkflowNode, validate_input_node_boundary
from .imports import import_specs_from_frontmatter
from .models import NodeSpec, ParsedWorkflow


def parsed_workflow_from_markdown(
    document: ParsedWorkflowMarkdown,
    source_path: Path,
) -> ParsedWorkflow:
    nodes = tuple(
        NodeSpec(
            payload=WorkflowNode.model_validate(node_payload),
            source_path=source_path,
            source_span=document.node_source_spans.get(node_payload["id"]),
            prompt_segment_spans=tuple(
                document.prompt_segment_spans.get(node_payload["id"], [])
            ),
        )
        for node_payload in document.payload["nodes"]
    )
    workflow_inputs = validate_declared_workflow_inputs(
        source_path,
        document.frontmatter.inputs,
        nodes,
    )
    return ParsedWorkflow(
        path=source_path,
        schema_version=document.frontmatter.schema_version,
        name=document.frontmatter.name,
        description=document.frontmatter.description or "",
        inputs=workflow_inputs,
        worktrees=dict(document.frontmatter.worktrees),
        imports=import_specs_from_frontmatter(
            document.frontmatter.imports, source_path
        ),
        nodes=nodes,
    )


def validate_declared_workflow_inputs(
    source_path: Path,
    workflow_inputs: dict[str, str],
    nodes: tuple[NodeSpec, ...],
) -> dict[str, str]:
    if not workflow_inputs:
        return {}

    nodes_by_id = {node.payload.id: node for node in nodes}
    for input_name, node_id in sorted(workflow_inputs.items()):
        node = nodes_by_id.get(node_id)
        if node is None:
            raise ValueError(
                f"Workflow '{source_path}' input '{input_name}' references "
                f"unknown node '{node_id}'."
            )
        validate_declared_input_node(
            node_payload=node.payload,
            source_path=source_path,
            input_name=input_name,
            node_id=node_id,
        )
    return dict(workflow_inputs)


def validate_declared_input_node(
    node_payload: WorkflowNode,
    source_path: Path,
    input_name: str,
    node_id: str,
) -> None:
    mode = node_payload.mode
    if mode != "input":
        raise ValueError(
            f"Workflow '{source_path}' input '{input_name}' must reference an input node; "
            f"'{node_id}' is '{mode}'."
        )

    validate_input_node_boundary(
        node_payload,
        f"Workflow '{source_path}' input node '{node_id}'",
    )
