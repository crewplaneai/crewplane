from __future__ import annotations

from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.diagnostics import (
    WorkflowDiagnosticSeverity,
    WorkflowValidationDiagnostic,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    INPUT_NODE_CONTRACT_RULES,
    WorkflowNode,
    render_prompt_for_role,
)
from crewplane.core.workflow.syntax import INPUT_SOURCE_PATTERN
from crewplane.core.workflow.validation.templates import extract_template_tokens

WORKFLOW_STRUCTURE_CODE = "WORKFLOW-STRUCTURE"
REFERENCE_PHASE = "reference"
STATIC_REVIEW_CONTEXT_ARTIFACTS = frozenset(
    {
        "output",
        "findings",
        "output_path",
        "findings_path",
        "output_size",
        "findings_size",
        "output_sha256",
        "findings_sha256",
    }
)


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
    diagnostics.extend(
        _prompt_segment_role_diagnostics(
            node,
            {PromptSegmentRole.SHARED, PromptSegmentRole.EXECUTOR},
        )
    )
    diagnostics.extend(_prompt_presence_diagnostics(node, PromptSegmentRole.EXECUTOR))
    reviewer_providers = [
        provider.provider
        for provider in node.providers
        if provider.role == ProviderRole.REVIEWER
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
    if _explicit_review_starts_with(node):
        diagnostics.append(
            _structure_diagnostic(
                f"Parallel node '{node.id}' does not support review_starts_with; "
                "use sequential executor/reviewer review loops.",
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
        allowed_roles = {PromptSegmentRole.SHARED, PromptSegmentRole.EXECUTOR}
    else:
        allowed_roles = {
            PromptSegmentRole.SHARED,
            PromptSegmentRole.EXECUTOR,
            PromptSegmentRole.REVIEWER,
        }
    diagnostics.extend(_prompt_segment_role_diagnostics(node, allowed_roles))
    diagnostics.extend(_prompt_presence_diagnostics(node, PromptSegmentRole.EXECUTOR))
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
        diagnostics.extend(
            _prompt_presence_diagnostics(node, PromptSegmentRole.REVIEWER)
        )
    if _reviewer_first_missing_context_warning_applies(node):
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' starts with a reviewer but has no "
                "dependencies or static review context references. Add explicit "
                "review context with {{file:...}} or upstream artifact references "
                "when reviewers need concrete material to inspect.",
                node.id,
                "warning",
            )
        )
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
    if _explicit_review_starts_with(node):
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' has a single provider and does not "
                "support review_starts_with.",
                node.id,
            )
        )
    role = node.providers[0].role
    if role == ProviderRole.EXECUTOR:
        return tuple(diagnostics)
    diagnostics.append(
        _structure_diagnostic(
            f"Sequential node '{node.id}' has a single provider but role is "
            f"'{role}'. Role must be '{ProviderRole.EXECUTOR.value}' for "
            "single-provider nodes.",
            node.id,
        )
    )
    return tuple(diagnostics)


def _multi_sequential_provider_role_diagnostics(
    node: WorkflowNode,
) -> tuple[WorkflowValidationDiagnostic, ...]:
    diagnostics: list[WorkflowValidationDiagnostic] = []
    if node.providers[0].role != ProviderRole.EXECUTOR:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' must start with an executor provider.",
                node.id,
            )
        )

    reviewer_segment_started = False
    for provider in node.providers:
        if provider.role == ProviderRole.REVIEWER:
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
    if node.providers[-1].role != ProviderRole.REVIEWER:
        diagnostics.append(
            _structure_diagnostic(
                f"Sequential node '{node.id}' must end with a reviewer provider.",
                node.id,
            )
        )
    return tuple(diagnostics)


def _explicit_review_starts_with(node: WorkflowNode) -> bool:
    return "review_starts_with" in node.model_fields_set


def _reviewer_first_missing_context_warning_applies(node: WorkflowNode) -> bool:
    return (
        node.review_starts_with == "reviewer"
        and not node.needs
        and _has_sequential_review_loop_provider_shape(node)
        and not _has_static_review_context(
            render_prompt_for_role(node, PromptSegmentRole.REVIEWER)
        )
    )


def _has_sequential_review_loop_provider_shape(node: WorkflowNode) -> bool:
    if len(node.providers) < 2:
        return False
    if node.providers[0].role != ProviderRole.EXECUTOR:
        return False
    if node.providers[-1].role != ProviderRole.REVIEWER:
        return False
    reviewer_segment_started = False
    for provider in node.providers:
        if provider.role == ProviderRole.REVIEWER:
            reviewer_segment_started = True
            continue
        if reviewer_segment_started:
            return False
    return True


def _has_static_review_context(prompt: str) -> bool:
    for token in extract_template_tokens(prompt):
        token_body = token[2:-2].strip()
        if token_body.startswith("file:"):
            return True
        if "." not in token_body:
            continue
        if token_body.rsplit(".", 1)[1] in STATIC_REVIEW_CONTEXT_ARTIFACTS:
            return True
    return False


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
    severity: WorkflowDiagnosticSeverity = "error",
) -> WorkflowValidationDiagnostic:
    return WorkflowValidationDiagnostic(
        code=WORKFLOW_STRUCTURE_CODE,
        phase=REFERENCE_PHASE,
        message=message,
        severity=severity,
        node_id=node_id,
    )
