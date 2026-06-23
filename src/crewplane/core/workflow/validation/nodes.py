from __future__ import annotations

from crewplane.core.workflow.diagnostics import (
    WorkflowValidationDiagnostic,
    format_diagnostics,
)
from crewplane.core.workflow.keywords import RESERVED_RUN_ROOT_NAMES
from crewplane.core.workflow.models import WorkflowNode, WorkflowPlan
from crewplane.core.workflow.syntax import NODE_ID_PATTERN
from crewplane.core.workflow.validation.modes import (
    collect_node_mode_diagnostics,
)

WORKFLOW_STRUCTURE_CODE = "WORKFLOW-STRUCTURE"
REFERENCE_PHASE = "reference"


def validate_workflow_nodes(workflow: WorkflowPlan) -> None:
    diagnostics = collect_workflow_node_diagnostics(workflow)
    if diagnostics:
        raise ValueError(format_diagnostics(diagnostics))


def collect_workflow_node_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if not workflow.nodes:
        diagnostics.append(
            _structure_diagnostic("Workflow must contain at least one node.")
        )

    diagnostics.extend(_unique_node_id_diagnostics(workflow))
    diagnostics.extend(_dependency_diagnostics(workflow))
    diagnostics.extend(_input_binding_diagnostics(workflow))
    for node in workflow.nodes:
        diagnostics.extend(_node_id_diagnostics(node))
        diagnostics.extend(collect_node_mode_diagnostics(node))
    return tuple(diagnostics)


def _unique_node_id_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node in workflow.nodes:
        if node.id in seen:
            duplicates.add(node.id)
        seen.add(node.id)
    if not duplicates:
        return ()
    duplicate_labels = ", ".join(sorted(duplicates))
    return (
        _structure_diagnostic(
            f"Duplicate node IDs found: {duplicate_labels}",
            metadata={"duplicates": duplicate_labels},
        ),
    )


def _dependency_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    node_ids = {node.id for node in workflow.nodes}
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for node in workflow.nodes:
        for needed in node.needs:
            message = _dependency_error(node.id, needed, node_ids)
            if message is not None:
                diagnostics.append(_structure_diagnostic(message, node.id))
    return tuple(diagnostics)


def _dependency_error(
    node_id: str, dependency_id: str, node_ids: set[str]
) -> str | None:
    if dependency_id == node_id:
        return f"Node '{node_id}' cannot depend on itself."
    if dependency_id not in node_ids:
        return f"Node '{node_id}' depends on unknown node '{dependency_id}'."
    return None


def _input_binding_diagnostics(
    workflow: WorkflowPlan,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if not workflow.inputs:
        return ()
    nodes_by_id = {node.id: node for node in workflow.nodes}
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for input_name, node_id in sorted(workflow.inputs.items()):
        node = nodes_by_id.get(node_id)
        if node is None:
            diagnostics.append(
                _structure_diagnostic(
                    f"Workflow input '{input_name}' references unknown node '{node_id}'.",
                    node_id,
                    {"input_name": input_name},
                )
            )
            continue
        if node.mode != "input":
            diagnostics.append(
                _structure_diagnostic(
                    f"Workflow input '{input_name}' must reference an input node; "
                    f"'{node_id}' is '{node.mode}'.",
                    node_id,
                    {"input_name": input_name},
                )
            )
    return tuple(diagnostics)


def _node_id_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if not NODE_ID_PATTERN.fullmatch(node.id):
        diagnostics.append(
            _structure_diagnostic(
                f"Node id '{node.id}' is invalid. IDs must match '[a-z0-9._-]+'.",
                node.id,
            )
        )
    if node.id in {".", ".."}:
        diagnostics.append(
            _structure_diagnostic(
                f"Node id '{node.id}' is invalid. IDs cannot be '.' or '..'.",
                node.id,
            )
        )
    if node.id not in RESERVED_RUN_ROOT_NAMES:
        return tuple(diagnostics)
    reserved = ", ".join(sorted(RESERVED_RUN_ROOT_NAMES))
    diagnostics.append(
        _structure_diagnostic(
            f"Node id '{node.id}' is reserved. IDs cannot be any of: {reserved}.",
            node.id,
        )
    )
    return tuple(diagnostics)


def _structure_diagnostic(
    message: str,
    node_id: str | None = None,
    metadata: dict[str, str | int | bool | None] | None = None,
) -> WorkflowValidationDiagnostic:
    return WorkflowValidationDiagnostic(
        code=WORKFLOW_STRUCTURE_CODE,
        phase=REFERENCE_PHASE,
        message=message,
        node_id=node_id,
        metadata=metadata or {},
    )
