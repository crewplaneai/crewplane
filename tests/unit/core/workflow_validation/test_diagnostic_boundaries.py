from __future__ import annotations

from collections.abc import Callable

import pytest

from crewplane.core.config import Config
from crewplane.core.preflight.validation import (
    validate_preflight_audit_rounds,
    validate_preflight_provider_references,
    validate_preflight_token_budget,
    validate_preflight_workflow_references,
)
from crewplane.core.workflow.models import (
    WorkflowNode,
    WorkflowPlan,
    validate_input_node_contract,
)
from crewplane.core.workflow.validation import collect_workflow_validation_diagnostics
from crewplane.core.workflow.validation.modes import collect_node_mode_diagnostics
from crewplane.core.workflow.validation.nodes import validate_workflow_nodes
from crewplane.core.workflow.validation.policies import validate_provider_references
from crewplane.core.workflow.validation.templates import validate_prompt_templates
from tests.helpers.workspace_preflight import workspace_config


def node_with(**changes: object) -> WorkflowNode:
    payload: dict[str, object] = {
        "id": "review",
        "mode": "sequential",
        "providers": [{"provider": "codex"}],
        "prompt_segments": [{"role": "shared", "content": "Review carefully."}],
    }
    payload.update(changes)
    return WorkflowNode.model_validate(payload)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"mode": "input", "providers": [], "prompt_segments": [], "source": None},
            "requires a non-empty source",
        ),
        (
            {"mode": "input", "providers": [], "prompt_segments": [], "source": " "},
            "requires a non-empty source",
        ),
        ({"mode": "parallel", "depth": 1}, "does not support depth"),
        ({"mode": "parallel", "failure_threshold": -1}, "greater than or equal to 0"),
        (
            {"mode": "parallel", "failure_threshold": 1},
            "must be less than provider count",
        ),
        ({"audit_rounds": 0}, "audit_rounds must be greater than 0"),
        ({"depth": 0}, "depth must be greater than 0"),
        ({"failure_threshold": 0}, "does not support failure_threshold"),
        ({"providers": []}, "requires at least one provider"),
        (
            {"providers": [{"provider": "codex", "role": "reviewer"}]},
            "Role must be 'executor'",
        ),
        ({"id": "Review"}, "IDs must match"),
        ({"id": "."}, "IDs cannot be"),
        ({"id": ".."}, "IDs cannot be"),
    ],
)
def test_workflow_validation_reports_invalid_node_contracts(
    changes: dict[str, object], message: str
) -> None:
    node = node_with(**changes)
    workflow = WorkflowPlan(name="Invalid workflow", nodes=[node])

    diagnostics = collect_workflow_validation_diagnostics(workflow)

    matches = [
        diagnostic for diagnostic in diagnostics if message in diagnostic.message
    ]
    assert matches
    assert all(
        diagnostic.node_id == node.id and diagnostic.severity == "error"
        for diagnostic in matches
    )
    with pytest.raises(ValueError, match=message):
        validate_workflow_nodes(workflow)


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        (["executor"], "single provider"),
        (["reviewer", "reviewer"], "must start with an executor"),
        (["executor", "executor"], "must end with a reviewer"),
        (
            ["executor", "reviewer", "executor", "reviewer"],
            "contiguous executor segment",
        ),
    ],
)
def test_reviewer_first_requires_a_valid_executor_reviewer_provider_sequence(
    roles: list[str], message: str
) -> None:
    node = node_with(
        providers=[
            {"provider": f"agent-{index}", "role": role}
            for index, role in enumerate(roles)
        ],
        review_starts_with="reviewer",
    )

    diagnostics = collect_node_mode_diagnostics(node)

    assert any(message in diagnostic.message for diagnostic in diagnostics)
    assert all(diagnostic.severity == "error" for diagnostic in diagnostics)


def test_reviewer_first_environment_variable_is_not_static_review_material() -> None:
    node = node_with(
        providers=[{"provider": "codex"}, {"provider": "claude", "role": "reviewer"}],
        review_starts_with="reviewer",
        prompt_segments=[{"role": "shared", "content": "Review {{env:REQUEST}}"}],
    )

    diagnostics = collect_node_mode_diagnostics(node)

    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "warning"
    assert "no dependencies or static review context" in diagnostics[0].message


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{file: }}", "Template values must be non-empty"),
        ("{{FILE:context.md}}", "case-sensitive"),
        ("{{PARAM:context}}", "case-sensitive"),
        ("{{review.output}}", "cannot reference its own output"),
    ],
)
def test_prompt_template_validation_raises_specific_reference_errors(
    template: str, message: str
) -> None:
    workflow = WorkflowPlan(
        name="Invalid reference",
        nodes=[node_with(prompt_segments=[{"role": "shared", "content": template}])],
    )

    with pytest.raises(ValueError, match=message):
        validate_prompt_templates(workflow)


@pytest.mark.parametrize("needs", [["missing"], ["review"]])
def test_invalid_graph_does_not_create_a_secondary_upstream_reference_error(
    needs: list[str],
) -> None:
    workflow = WorkflowPlan(
        name="Invalid dependency",
        nodes=[
            node_with(id="upstream"),
            node_with(
                needs=needs,
                prompt_segments=[{"role": "shared", "content": "{{upstream.output}}"}],
            ),
        ],
    )

    diagnostics = collect_workflow_validation_diagnostics(workflow)

    assert diagnostics
    assert all(
        "not an upstream dependency" not in diagnostic.message
        for diagnostic in diagnostics
    )
    validate_prompt_templates(workflow)


@pytest.mark.parametrize(
    "source", [None, " ", "plain text", "{{file:one}} {{file:two}}"]
)
def test_input_node_contract_requires_exactly_one_file_reference(
    source: str | None,
) -> None:
    node = WorkflowNode(id="context", mode="input", source=source)
    message = (
        "requires a non-empty source"
        if source is None or not source.strip()
        else "must be exactly one raw"
    )

    with pytest.raises(ValueError, match=message):
        validate_input_node_contract(node, "Input context")


def test_input_node_contract_accepts_one_file_reference() -> None:
    validate_input_node_contract(
        WorkflowNode(id="context", mode="input", source="{{file:context.md}}"),
        "Input context",
    )


def test_reference_validators_accept_known_providers_and_valid_input_sources() -> None:
    workflow = WorkflowPlan(
        name="Valid workflow", nodes=[node_with(providers=[{"provider": "alpha"}])]
    )
    config = workspace_config({"enabled": False})

    validate_provider_references(workflow, config)
    validate_preflight_provider_references(workflow, config)
    validate_preflight_audit_rounds(workflow, config)
    validate_preflight_token_budget(workflow, config)
    validate_workflow_nodes(workflow)
    assert validate_preflight_workflow_references(workflow) is workflow


@pytest.mark.parametrize(
    "validate",
    [validate_provider_references, validate_preflight_provider_references],
    ids=["workflow-validation", "preflight-validation"],
)
def test_provider_reference_validators_identify_missing_provider_and_locations(
    validate: Callable[[WorkflowPlan, Config], None],
) -> None:
    workflow = WorkflowPlan(name="Missing provider", nodes=[node_with()])
    config = workspace_config({"enabled": False})

    with pytest.raises(ValueError, match="Unknown provider 'codex'") as caught:
        validate(workflow, config)

    assert "workflow 'Missing provider' -> node 'review'" in str(caught.value)


def test_workflow_validation_rejects_an_empty_node_list() -> None:
    with pytest.raises(ValueError, match="at least one node"):
        validate_preflight_workflow_references(WorkflowPlan(name="Empty", nodes=[]))


def test_workflow_input_binding_identifies_missing_node() -> None:
    workflow = WorkflowPlan(
        name="Invalid binding", inputs={"request": "missing"}, nodes=[node_with()]
    )

    diagnostics = collect_workflow_validation_diagnostics(workflow)

    assert len(diagnostics) == 1
    assert diagnostics[0].node_id == "missing"
    assert diagnostics[0].metadata == {"input_name": "request"}
    assert "references unknown node 'missing'" in diagnostics[0].message
