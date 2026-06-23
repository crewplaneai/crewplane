from __future__ import annotations

from dataclasses import dataclass

from crewplane.core.workflow.diagnostics import (
    WorkflowValidationDiagnostic,
    format_diagnostics,
)
from crewplane.core.workflow.keywords import (
    ALLOWED_NODE_ARTIFACT_NAME_SET,
    ALLOWED_NODE_ARTIFACT_NAMES,
)
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workflow.syntax import (
    COMPOSITION_ONLY_TEMPLATE_TYPES,
    KEY_VALUE_TEMPLATE_PATTERN,
    NODE_ARTIFACT_REFERENCE_PATTERN,
    SUPPORTED_TEMPLATE_TYPES,
    TEMPLATE_TOKEN_PATTERN,
)

WORKFLOW_TEMPLATE_CODE = "WORKFLOW-TEMPLATE"
REFERENCE_PHASE = "reference"


@dataclass(frozen=True)
class NodeArtifactReference:
    node_id: str
    artifact_name: str


def extract_template_tokens(prompt: str) -> list[str]:
    return [match.group(0) for match in TEMPLATE_TOKEN_PATTERN.finditer(prompt)]


def validate_prompt_templates(workflow: WorkflowPlan) -> None:
    diagnostics = collect_prompt_template_diagnostics(workflow)
    if diagnostics:
        raise ValueError(format_diagnostics(diagnostics))


def collect_prompt_template_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    nodes_by_id = {node.id: node for node in workflow.nodes}
    ancestors = _ancestor_map_if_available(workflow)
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for node in workflow.nodes:
        for template_text in _node_template_texts(node):
            for template_token in extract_template_tokens(template_text):
                message = _template_reference_error(
                    node.id,
                    template_token,
                    nodes_by_id,
                    ancestors.get(node.id) if ancestors is not None else None,
                )
                if message is not None:
                    diagnostics.append(_template_diagnostic(message, node.id))
    return tuple(diagnostics)


def _node_template_texts(node: WorkflowNode) -> tuple[str, ...]:
    values: list[str] = [segment.content for segment in node.prompt_segments]
    if node.source:
        values.append(node.source)
    return tuple(values)


def _template_reference_error(
    node_id: str,
    template_token: str,
    nodes_by_id: dict[str, WorkflowNode],
    ancestors: set[str] | None,
) -> str | None:
    artifact_match = NODE_ARTIFACT_REFERENCE_PATTERN.fullmatch(template_token)
    if artifact_match is not None:
        return _artifact_reference_error(
            node_id,
            NodeArtifactReference(
                node_id=artifact_match.group(1),
                artifact_name=artifact_match.group(2),
            ),
            nodes_by_id,
            ancestors,
        )

    key_value_match = KEY_VALUE_TEMPLATE_PATTERN.fullmatch(template_token)
    if key_value_match is None:
        return _unsupported_template_error(node_id, template_token)

    template_type = key_value_match.group(1)
    if template_type in SUPPORTED_TEMPLATE_TYPES:
        if key_value_match.group(2).strip():
            return None
        return (
            f"Node '{node_id}' references invalid template '{template_token}'. "
            "Template values must be non-empty."
        )
    if template_type in COMPOSITION_ONLY_TEMPLATE_TYPES:
        return (
            f"Node '{node_id}' references composition-only template "
            f"'{template_token}'. {{param:KEY}} templates are only valid in "
            "Markdown workflow composition and must be resolved before runtime "
            "validation."
        )

    if (
        template_type.lower() in SUPPORTED_TEMPLATE_TYPES
        or template_type.lower() in COMPOSITION_ONLY_TEMPLATE_TYPES
    ):
        return (
            f"Node '{node_id}' references unsupported template '{template_token}'. "
            "Template types are case-sensitive and must be lower-case exactly."
        )
    return _unsupported_template_error(node_id, template_token)


def _unsupported_template_error(node_id: str, template_token: str) -> str:
    return (
        f"Node '{node_id}' references unsupported template '{template_token}'. "
        "Supported forms are {{env:KEY}}, {{file:PATH}}, {{var:KEY}}, "
        "{{node.output}}, {{node.findings}}, {{node.output_path}}, and "
        "{{node.findings_path}}, plus {{node.output_size}}, "
        "{{node.findings_size}}, {{node.output_sha256}}, and "
        "{{node.findings_sha256}} for artifact metadata. {{param:KEY}} is only "
        "valid during Markdown workflow composition."
    )


def _artifact_reference_error(
    node_id: str,
    reference: NodeArtifactReference,
    nodes_by_id: dict[str, WorkflowNode],
    ancestors: set[str] | None,
) -> str | None:
    reference_id = reference.node_id
    artifact_name = reference.artifact_name
    artifact_template = f"{{{{{reference_id}.{artifact_name}}}}}"
    keyword_error = _node_artifact_name_error(artifact_name)
    if keyword_error is not None:
        return (
            f"Node '{node_id}' references unsupported artifact "
            f"'{artifact_template}'. {keyword_error}"
        )
    referenced_node = nodes_by_id.get(reference_id)
    if referenced_node is None:
        return (
            f"Node '{node_id}' references unknown {artifact_name} "
            f"'{artifact_template}'."
        )
    if reference_id == node_id:
        return (
            f"Node '{node_id}' cannot reference its own {artifact_name} "
            f"'{artifact_template}'."
        )
    if ancestors is None:
        return None
    if reference_id not in ancestors:
        return (
            f"Node '{node_id}' references '{artifact_template}' "
            f"but '{reference_id}' is not an upstream dependency."
        )
    if not artifact_name.startswith("findings"):
        return None
    if referenced_node.findings:
        return None
    return (
        f"Node '{node_id}' references '{artifact_template}' but upstream node "
        f"'{reference_id}' does not define findings: true."
    )


def _ancestor_map_if_available(
    workflow: WorkflowPlan,
) -> dict[str, set[str]] | None:
    node_ids = {node.id for node in workflow.nodes}
    if len(node_ids) != len(workflow.nodes):
        return None
    dependencies = {node.id: set(node.needs) for node in workflow.nodes}
    if any(node_id in needs for node_id, needs in dependencies.items()):
        return None
    if any(not needs <= node_ids for needs in dependencies.values()):
        return None

    ancestors: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    pending = dict(dependencies)
    while pending:
        ready = [node_id for node_id, needs in pending.items() if not needs]
        if not ready:
            return None
        for node_id in ready:
            pending.pop(node_id)
            for dependent_id, needs in pending.items():
                if node_id in needs:
                    ancestors[dependent_id].update(ancestors[node_id])
                    ancestors[dependent_id].add(node_id)
                    needs.remove(node_id)
    return ancestors


def _node_artifact_name_error(artifact_name: str) -> str | None:
    if artifact_name in ALLOWED_NODE_ARTIFACT_NAME_SET:
        return None
    allowed = ", ".join(ALLOWED_NODE_ARTIFACT_NAMES)
    if artifact_name.lower() in ALLOWED_NODE_ARTIFACT_NAME_SET:
        return f"node artifact name must be lower-case and one of: {allowed}"
    return f"node artifact name must be one of: {allowed}"


def _template_diagnostic(
    message: str,
    node_id: str,
) -> WorkflowValidationDiagnostic:
    return WorkflowValidationDiagnostic(
        code=WORKFLOW_TEMPLATE_CODE,
        phase=REFERENCE_PHASE,
        message=message,
        node_id=node_id,
    )
