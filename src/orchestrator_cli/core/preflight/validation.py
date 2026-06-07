from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator_cli.core.config import Config
from orchestrator_cli.core.prompt_segments import PromptSegmentRole
from orchestrator_cli.core.token_budget import resolve_token_budget
from orchestrator_cli.core.workflow_graph import ancestor_map, topological_waves
from orchestrator_cli.core.workflow_keywords import (
    ALLOWED_NODE_ARTIFACT_NAME_SET,
    ALLOWED_NODE_ARTIFACT_NAMES,
    RESERVED_RUN_ROOT_NAMES,
    validate_exact_keyword,
)
from orchestrator_cli.core.workflow_models import (
    WorkflowNode,
    WorkflowPlan,
    render_prompt_for_role,
    validate_input_node_contract,
)

NODE_ID_PATTERN = re.compile(r"^[a-z0-9._-]+$")
ARTIFACT_REFERENCE_PATTERN = re.compile(
    r"\{\{\s*([a-z0-9._-]+)\.([A-Za-z0-9_-]+)\s*\}\}"
)
TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{[^{}]*\}\}")
KEY_VALUE_TEMPLATE_PATTERN = re.compile(r"\{\{([a-zA-Z_]+):([^}]+)\}\}")
SUPPORTED_TEMPLATE_TYPES = frozenset({"env", "file", "var"})
COMPOSITION_ONLY_TEMPLATE_TYPES = frozenset({"param"})


@dataclass(frozen=True)
class NodeArtifactReference:
    node_id: str
    artifact_name: str


def validate_preflight_workflow_references(workflow: WorkflowPlan) -> WorkflowPlan:
    if not workflow.nodes:
        raise ValueError("Workflow must contain at least one node.")

    _validate_unique_node_ids(workflow)
    _validate_dependencies_exist(workflow)
    _validate_input_bindings(workflow)

    for node in workflow.nodes:
        _validate_node_id_format(node)
        _validate_dot_segment_node_id(node)
        _validate_reserved_node_id(node)
        _validate_node_mode(node)

    topological_waves(workflow)
    _validate_prompt_templates(workflow)
    return workflow


def validate_preflight_provider_references(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    errors: list[str] = []
    for provider_name, locations in _missing_provider_locations(
        workflow,
        config,
    ).items():
        provider_locations = ", ".join(sorted(set(locations)))
        errors.append(
            f"Unknown provider '{provider_name}' referenced at: {provider_locations}."
        )
    if errors:
        raise ValueError("\n".join(errors))


def validate_preflight_audit_rounds(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    max_audit_rounds = config.settings.max_audit_rounds if config.settings else 5
    errors = [
        (
            f"Sequential node '{node.id}' audit_rounds ({node.audit_rounds}) must be "
            f"less than or equal to settings.max_audit_rounds ({max_audit_rounds})."
        )
        for node in workflow.nodes
        if node.audit_rounds is not None and node.audit_rounds > max_audit_rounds
    ]
    if errors:
        raise ValueError("\n".join(errors))


def validate_preflight_token_budget(
    workflow: WorkflowPlan,
    config: Config,
) -> None:
    errors: list[str] = []
    settings_budget = (
        config.settings.token_budget if config.settings is not None else None
    )
    for node in workflow.nodes:
        if node.mode == "input":
            continue
        try:
            resolve_token_budget(settings_budget, node.token_budget)
        except ValueError as exc:
            errors.append(f"Node '{node.id}' token_budget is invalid: {exc}")
    if errors:
        raise ValueError("\n".join(errors))


def _missing_provider_locations(
    workflow: WorkflowPlan,
    config: Config,
) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    for node in workflow.nodes:
        for provider in node.providers:
            if provider.provider in config.agents:
                continue
            location = f"workflow '{workflow.name}' -> node '{node.id}'"
            missing.setdefault(provider.provider, []).append(location)
    return missing


def _require_prompt_for_role(node: WorkflowNode, role: PromptSegmentRole) -> None:
    if render_prompt_for_role(node, role).strip():
        return
    raise ValueError(f"Node '{node.id}' rendered {role} prompt cannot be empty.")


def _validate_prompt_segment_roles(
    node: WorkflowNode,
    allowed_roles: set[PromptSegmentRole],
) -> None:
    for segment in node.prompt_segments:
        if segment.role in allowed_roles:
            continue
        allowed_roles_label = ", ".join(sorted(allowed_roles))
        raise ValueError(
            f"Node '{node.id}' uses disallowed prompt segment role "
            f"'{segment.role}'. Allowed roles: {allowed_roles_label}."
        )


def _require_providers(node: WorkflowNode, mode_label: str) -> None:
    if node.providers:
        return
    raise ValueError(f"{mode_label} node '{node.id}' requires at least one provider.")


def _validate_parallel_node(node: WorkflowNode) -> None:
    _require_providers(node, "Parallel")
    _validate_prompt_segment_roles(node, {"shared", "executor"})
    _require_prompt_for_role(node, "executor")
    reviewer_providers = [
        provider.provider for provider in node.providers if provider.role == "reviewer"
    ]
    if reviewer_providers:
        provider_list = ", ".join(reviewer_providers)
        raise ValueError(
            f"Parallel node '{node.id}' does not allow reviewer roles. "
            f"Reviewer providers: {provider_list}."
        )
    if node.depth is not None:
        raise ValueError(
            f"Parallel node '{node.id}' does not support depth; use sequential mode."
        )
    if node.audit_rounds is not None:
        raise ValueError(
            f"Parallel node '{node.id}' does not support audit_rounds; use sequential mode."
        )
    _validate_parallel_failure_threshold(node)


def _validate_input_node(node: WorkflowNode) -> None:
    validate_input_node_contract(node, f"Input node '{node.id}'")


def _validate_parallel_failure_threshold(node: WorkflowNode) -> None:
    failure_threshold = node.failure_threshold
    if failure_threshold is None:
        return
    if failure_threshold < 0:
        raise ValueError(
            f"Parallel node '{node.id}' failure_threshold must be greater than or equal to 0."
        )
    if failure_threshold >= len(node.providers):
        raise ValueError(
            f"Parallel node '{node.id}' failure_threshold ({failure_threshold}) "
            f"must be less than provider count ({len(node.providers)})."
        )


def _validate_sequential_node(node: WorkflowNode) -> None:
    _require_providers(node, "Sequential")
    allowed_roles: set[PromptSegmentRole]
    if len(node.providers) == 1:
        allowed_roles = {"shared", "executor"}
    else:
        allowed_roles = {"shared", "executor", "reviewer"}
    _validate_prompt_segment_roles(node, allowed_roles)
    _require_prompt_for_role(node, "executor")
    if node.audit_rounds is not None and node.audit_rounds <= 0:
        raise ValueError(
            f"Sequential node '{node.id}' audit_rounds must be greater than 0 when provided."
        )
    if node.depth is not None and node.depth <= 0:
        raise ValueError(
            f"Sequential node '{node.id}' depth must be greater than 0 when provided."
        )
    if node.failure_threshold is not None:
        raise ValueError(
            f"Sequential node '{node.id}' does not support failure_threshold."
        )

    _validate_sequential_provider_roles(node)
    if len(node.providers) > 1:
        _require_prompt_for_role(node, "reviewer")


def _validate_sequential_provider_roles(node: WorkflowNode) -> None:
    if len(node.providers) == 1:
        _validate_single_sequential_provider_role(node)
        return
    _validate_multi_sequential_provider_roles(node)


def _validate_single_sequential_provider_role(node: WorkflowNode) -> None:
    if node.audit_rounds is not None:
        raise ValueError(
            f"Sequential node '{node.id}' has a single provider and does not support audit_rounds."
        )
    if node.providers[0].role == "executor":
        return
    raise ValueError(
        f"Sequential node '{node.id}' has a single provider but role is "
        f"'{node.providers[0].role}'. Role must be 'executor' for single-provider nodes."
    )


def _validate_multi_sequential_provider_roles(node: WorkflowNode) -> None:
    if node.providers[0].role != "executor":
        raise ValueError(
            f"Sequential node '{node.id}' must start with an executor provider."
        )

    reviewer_segment_started = False
    for provider in node.providers:
        if provider.role == "reviewer":
            reviewer_segment_started = True
            continue
        if reviewer_segment_started:
            raise ValueError(
                f"Sequential node '{node.id}' must declare providers as a contiguous "
                "executor segment followed by a contiguous reviewer segment."
            )
    if node.providers[-1].role != "reviewer":
        raise ValueError(
            f"Sequential node '{node.id}' must end with a reviewer provider."
        )


def _validate_node_id_format(node: WorkflowNode) -> None:
    if NODE_ID_PATTERN.fullmatch(node.id):
        return
    raise ValueError(f"Node id '{node.id}' is invalid. IDs must match '[a-z0-9._-]+'.")


def _validate_dot_segment_node_id(node: WorkflowNode) -> None:
    if node.id not in {".", ".."}:
        return
    raise ValueError(f"Node id '{node.id}' is invalid. IDs cannot be '.' or '..'.")


def _validate_reserved_node_id(node: WorkflowNode) -> None:
    if node.id not in RESERVED_RUN_ROOT_NAMES:
        return
    reserved = ", ".join(sorted(RESERVED_RUN_ROOT_NAMES))
    raise ValueError(
        f"Node id '{node.id}' is reserved. IDs cannot be any of: {reserved}."
    )


def _validate_unique_node_ids(workflow: WorkflowPlan) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node in workflow.nodes:
        if node.id in seen:
            duplicates.add(node.id)
        seen.add(node.id)
    if duplicates:
        raise ValueError(f"Duplicate node IDs found: {', '.join(sorted(duplicates))}")


def _validate_dependencies_exist(workflow: WorkflowPlan) -> None:
    node_ids = {node.id for node in workflow.nodes}
    errors: list[str] = []
    for node in workflow.nodes:
        for needed in node.needs:
            error = _dependency_error(node.id, needed, node_ids)
            if error is not None:
                errors.append(error)
    if errors:
        raise ValueError("\n".join(errors))


def _dependency_error(
    node_id: str,
    dependency_id: str,
    node_ids: set[str],
) -> str | None:
    if dependency_id == node_id:
        return f"Node '{node_id}' cannot depend on itself."
    if dependency_id not in node_ids:
        return f"Node '{node_id}' depends on unknown node '{dependency_id}'."
    return None


def _validate_prompt_templates(workflow: WorkflowPlan) -> None:
    nodes_by_id = {node.id: node for node in workflow.nodes}
    ancestors = ancestor_map(workflow)
    errors: list[str] = []
    for node in workflow.nodes:
        for template_text in _node_template_texts(node):
            for template_token in _extract_template_tokens(template_text):
                error = _template_reference_error(
                    node.id,
                    template_token,
                    nodes_by_id,
                    ancestors[node.id],
                )
                if error is not None:
                    errors.append(error)
    if errors:
        raise ValueError("\n".join(errors))


def _node_template_texts(node: WorkflowNode) -> tuple[str, ...]:
    values: list[str] = [segment.content for segment in node.prompt_segments]
    if node.source:
        values.append(node.source)
    return tuple(values)


def _extract_template_tokens(prompt: str) -> list[str]:
    return [match.group(0) for match in TEMPLATE_TOKEN_PATTERN.finditer(prompt)]


def _template_reference_error(
    node_id: str,
    template_token: str,
    nodes_by_id: dict[str, WorkflowNode],
    ancestors: set[str],
) -> str | None:
    artifact_match = ARTIFACT_REFERENCE_PATTERN.fullmatch(template_token)
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


def _validate_input_bindings(workflow: WorkflowPlan) -> None:
    if not workflow.inputs:
        return
    nodes_by_id = {node.id: node for node in workflow.nodes}
    errors: list[str] = []
    for input_name, node_id in sorted(workflow.inputs.items()):
        node = nodes_by_id.get(node_id)
        if node is None:
            errors.append(
                f"Workflow input '{input_name}' references unknown node '{node_id}'."
            )
            continue
        if node.mode != "input":
            errors.append(
                f"Workflow input '{input_name}' must reference an input node; "
                f"'{node_id}' is '{node.mode}'."
            )
    if errors:
        raise ValueError("\n".join(errors))


def _artifact_reference_error(
    node_id: str,
    reference: NodeArtifactReference,
    nodes_by_id: dict[str, WorkflowNode],
    ancestors: set[str],
) -> str | None:
    reference_id = reference.node_id
    artifact_name = reference.artifact_name
    artifact_template = f"{{{{{reference_id}.{artifact_name}}}}}"
    try:
        validate_exact_keyword(
            artifact_name,
            field_name="node artifact name",
            allowed_values=ALLOWED_NODE_ARTIFACT_NAMES,
            allowed_value_set=ALLOWED_NODE_ARTIFACT_NAME_SET,
        )
    except ValueError as exc:
        return (
            f"Node '{node_id}' references unsupported artifact "
            f"'{artifact_template}'. {exc}"
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


def _validate_node_mode(node: WorkflowNode) -> None:
    if node.mode == "input":
        _validate_input_node(node)
        return
    if node.source is not None:
        raise ValueError(f"Node '{node.id}' source is only valid for input nodes.")
    if node.mode == "parallel":
        _validate_parallel_node(node)
        return
    _validate_sequential_node(node)
