from __future__ import annotations

from orchestrator_cli.core.prompt_segments import PromptSegmentRole
from orchestrator_cli.core.workflow_diagnostics import WorkflowValidationDiagnostic
from orchestrator_cli.core.workflow_models import (
    INPUT_NODE_CONTRACT_RULES,
    WorkflowNode,
    render_prompt_for_role,
)
from orchestrator_cli.core.workflow_syntax import INPUT_SOURCE_PATTERN

WORKFLOW_STRUCTURE_CODE = "WORKFLOW-STRUCTURE"
REFERENCE_PHASE = "reference"


def collect_node_mode_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if node.mode == "input":
        return _input_node_diagnostics(node)
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if node.source is not None:
        diagnostics.append(
            _structure_diagnostic(
                f"Node '{node.id}' source is only valid for input nodes.",
                node.id,
            )
        )
    if node.mode == "parallel":
        diagnostics.extend(_parallel_node_diagnostics(node))
    else:
        diagnostics.extend(_sequential_node_diagnostics(node))
    return tuple(diagnostics)


def _input_node_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    node_label = f"Input node '{node.id}'"
    diagnostics = [
        _structure_diagnostic(
            f"{node_label} must not define {rule.field_name}.",
            node.id,
        )
        for rule in INPUT_NODE_CONTRACT_RULES
        if rule.is_defined(node)
    ]
    source = node.source
    if source is None or not source.strip():
        diagnostics.append(
            _structure_diagnostic(f"{node_label} requires a non-empty source.", node.id)
        )
    elif not INPUT_SOURCE_PATTERN.fullmatch(source.strip()):
        diagnostics.append(
            _structure_diagnostic(
                f"{node_label} source must be exactly one raw "
                "{{file:...}} template.",
                node.id,
            )
        )
    return tuple(diagnostics)


def _parallel_node_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    diagnostics.extend(_provider_presence_diagnostics(node, "Parallel"))
    diagnostics.extend(_prompt_segment_role_diagnostics(node, {"shared", "executor"}))
    diagnostics.extend(_prompt_presence_diagnostics(node, "executor"))
    reviewer_providers = [
        provider.provider for provider in node.providers if provider.role == "reviewer"
    ]
    if reviewer_providers:
        provider_list = ", ".join(reviewer_providers)
        diagnostics.append(
            _structure_diagnostic(
                f"Parallel node '{node.id}' does not allow reviewer roles. "
                f"Reviewer providers: {provider_list}.",
                node.id,
            )
        )
    if node.depth is not None:
        diagnostics.append(
            _structure_diagnostic(
                f"Parallel node '{node.id}' does not support depth; "
                "use sequential mode.",
                node.id,
            )
        )
    if node.audit_rounds is not None:
        diagnostics.append(
            _structure_diagnostic(
                f"Parallel node '{node.id}' does not support audit_rounds; "
                "use sequential mode.",
                node.id,
            )
        )
    diagnostics.extend(_parallel_failure_threshold_diagnostics(node))
    return tuple(diagnostics)


def _parallel_failure_threshold_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    failure_threshold = node.failure_threshold
    if failure_threshold is None:
        return ()
    if failure_threshold < 0:
        return (
            _structure_diagnostic(
                f"Parallel node '{node.id}' failure_threshold must be greater than "
                "or equal to 0.",
                node.id,
            ),
        )
    if failure_threshold >= len(node.providers):
        return (
            _structure_diagnostic(
                f"Parallel node '{node.id}' failure_threshold ({failure_threshold}) "
                f"must be less than provider count ({len(node.providers)}).",
                node.id,
            ),
        )
    return ()


def _sequential_node_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    diagnostics.extend(_provider_presence_diagnostics(node, "Sequential"))
    allowed_roles: set[PromptSegmentRole]
    if len(node.providers) == 1:
        allowed_roles = {"shared", "executor"}
    else:
        allowed_roles = {"shared", "executor", "reviewer"}
    diagnostics.extend(_prompt_segment_role_diagnostics(node, allowed_roles))
    diagnostics.extend(_prompt_presence_diagnostics(node, "executor"))
    if node.audit_rounds is not None and node.audit_rounds <= 0:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' audit_rounds must be greater than 0 "
                "when provided.",
                node.id,
            )
        )
    if node.depth is not None and node.depth <= 0:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' depth must be greater than 0 "
                "when provided.",
                node.id,
            )
        )
    if node.failure_threshold is not None:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' does not support failure_threshold.",
                node.id,
            )
        )

    diagnostics.extend(_sequential_provider_role_diagnostics(node))
    if len(node.providers) > 1:
        diagnostics.extend(_prompt_presence_diagnostics(node, "reviewer"))
    return tuple(diagnostics)


def _sequential_provider_role_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if not node.providers:
        return ()
    if len(node.providers) == 1:
        return _single_sequential_provider_role_diagnostics(node)
    return _multi_sequential_provider_role_diagnostics(node)


def _single_sequential_provider_role_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if node.audit_rounds is not None:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' has a single provider and does not "
                "support audit_rounds.",
                node.id,
            )
        )
    role = node.providers[0].role
    if role == "executor":
        return tuple(diagnostics)
    diagnostics.append(
        _structure_diagnostic(
            f"Sequential node '{node.id}' has a single provider but role is "
            f"'{role}'. Role must be 'executor' for single-provider nodes.",
            node.id,
        )
    )
    return tuple(diagnostics)


def _multi_sequential_provider_role_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if node.providers[0].role != "executor":
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' must start with an executor provider.",
                node.id,
            )
        )

    reviewer_segment_started = False
    for provider in node.providers:
        if provider.role == "reviewer":
            reviewer_segment_started = True
            continue
        if reviewer_segment_started:
            diagnostics.append(
                _structure_diagnostic(
                    f"Sequential node '{node.id}' must declare providers as a "
                    "contiguous executor segment followed by a contiguous reviewer "
                    "segment.",
                    node.id,
                )
            )
            break
    if node.providers[-1].role != "reviewer":
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' must end with a reviewer provider.",
                node.id,
            )
        )
    return tuple(diagnostics)


def _prompt_segment_role_diagnostics(
    node: WorkflowNode,
    allowed_roles: set[PromptSegmentRole],
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    for segment in node.prompt_segments:
        if segment.role in allowed_roles:
            continue
        allowed_roles_label = ", ".join(sorted(allowed_roles))
        diagnostics.append(
            _structure_diagnostic(
                f"Node '{node.id}' uses disallowed prompt segment role "
                f"'{segment.role}'. Allowed roles: {allowed_roles_label}.",
                node.id,
            )
        )
    return tuple(diagnostics)


def _prompt_presence_diagnostics(
    node: WorkflowNode,
    role: PromptSegmentRole,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if render_prompt_for_role(node, role).strip():
        return ()
    return (
        _structure_diagnostic(
            f"Node '{node.id}' rendered {role} prompt cannot be empty.",
            node.id,
        ),
    )


def _provider_presence_diagnostics(
    node: WorkflowNode,
    mode_label: str,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    if node.providers:
        return ()
    return (
        _structure_diagnostic(
            f"{mode_label} node '{node.id}' requires at least one provider.",
            node.id,
        ),
    )


def _structure_diagnostic(
    message: str,
    node_id: str,
) -> WorkflowValidationDiagnostic:
    return WorkflowValidationDiagnostic(
        code=WORKFLOW_STRUCTURE_CODE,
        phase=REFERENCE_PHASE,
        message=message,
        node_id=node_id,
    )
