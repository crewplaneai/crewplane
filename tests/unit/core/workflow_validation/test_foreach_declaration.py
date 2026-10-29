import copy

import pytest

from crewplane.core.workflow.collections import COLLECTION_BYTE_LIMIT
from crewplane.core.workflow.markdown.models import WorkflowNodeConfig
from crewplane.core.workflow.models import WorkflowNode, workflow_node_payload_dict
from crewplane.core.workflow.validation.foreach import validate_foreach_declaration


def declaration(**overrides: object) -> dict[str, object]:
    return {
        "id": "group",
        "mode": "foreach",
        "workflow": "child.md",
        "item_input": "item",
        "items": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "value", [None, "foreach", [], {"id": "plain", "mode": "input"}]
)
def test_passthrough_preserves_identity(value: object) -> None:
    assert validate_foreach_declaration(value) is value


def test_normalization_copies_bindings_and_preserves_other_values() -> None:
    item = {"text": "{{file:literal}}"}
    items = [item, item]
    needs = ["second", "first"]
    inputs = {" second ": " node-b ", " first ": " node-a "}
    parameters = {" topic ": " {{param:topic}} "}
    internal_parameters = {" count ": " 2 "}
    value = declaration(
        workflow=" child.md",
        item_input=" item ",
        items=items,
        needs=needs,
        inputs=inputs,
        with_params=internal_parameters,
        **{"with": parameters},
    )
    original = copy.deepcopy(value)

    result = validate_foreach_declaration(value, internal_parameters=True)

    assert isinstance(result, dict)
    assert result is not value
    assert result == {
        **value,
        "item_input": "item",
        "inputs": {"second": "node-b", "first": "node-a"},
        "with": {"topic": " {{param:topic}} "},
        "with_params": {"count": " 2 "},
    }
    assert list(result) == list(value)
    assert list(result["inputs"]) == ["second", "first"]
    assert result["inputs"] is not inputs
    assert result["with"] is not parameters
    assert result["with_params"] is not internal_parameters
    assert result["items"] is items
    assert items[0] is items[1] is item
    assert result["needs"] is needs
    assert value == original


def test_reference_whitespace_is_validated_without_normalization() -> None:
    reference = " {{planner.findings}} "

    result = validate_foreach_declaration(declaration(items=reference, max_items=3))

    assert isinstance(result, dict)
    assert result["items"] is reference


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
@pytest.mark.parametrize("items", [[], [{"x": 1}, {"x": 1}]])
def test_literal_cap_omission_survives_model_validation(model, items) -> None:
    node = model.model_validate(declaration(items=items))
    assert node.items == items
    assert node.max_items is None
    assert "max_items" not in node.model_fields_set


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
@pytest.mark.parametrize("items", [[], [{}], "{{a.output}}", "{{a.findings}}"])
@pytest.mark.parametrize("limit", [None, True, False, "3", 3.0, 1.5, 0, -1])
def test_supplied_caps_are_strict_positive_integers(model, items, limit) -> None:
    with pytest.raises(ValueError, match="strict positive integer"):
        model.model_validate(declaration(items=items, max_items=limit))


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
@pytest.mark.parametrize("items", ["{{a.output}}", "{{a.findings}}"])
def test_references_require_explicit_caps(model, items) -> None:
    with pytest.raises(ValueError, match="requires max_items"):
        model.model_validate(declaration(items=items))


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
@pytest.mark.parametrize("items", [None, {}, 1, True, ()])
def test_invalid_items_without_cap_raise_validation_error(model, items) -> None:
    with pytest.raises(ValueError, match="array of objects"):
        model.model_validate(declaration(items=items))
    value = declaration()
    del value["items"]
    with pytest.raises(ValueError, match="array of objects"):
        model.model_validate(value)


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
@pytest.mark.parametrize(
    "items",
    [
        [1],
        [{1: "key"}],
        [{"x": float("nan")}],
        [{"x": float("inf")}],
        [{"x": b"bytes"}],
        [{"x": {1}}],
        [{"x": "\ud800"}],
        [{"\ud800": 1}],
        [{"x": "x" * COLLECTION_BYTE_LIMIT}],
    ],
)
def test_omission_preserves_raw_collection_safety(model, items) -> None:
    with pytest.raises(ValueError, match="Foreach 'group' items"):
        model.model_validate(declaration(items=items))


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
def test_omission_rejects_excessive_depth_and_cycles(model) -> None:
    nested = {}
    for depth in range(64):
        nested = {str(depth): nested}
    with pytest.raises(ValueError, match="depth"):
        model.model_validate(declaration(items=[nested]))
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cycle"):
        model.model_validate(declaration(items=[cyclic]))


@pytest.mark.parametrize("items", [[], [{}], "{{a.output}}", "{{a.findings}}"])
def test_explicit_cap_survives_payload_roundtrip(items) -> None:
    node = WorkflowNode.model_validate(declaration(items=items, max_items=3))
    payload = workflow_node_payload_dict(node)
    assert payload["max_items"] == 3
    assert WorkflowNode.model_validate(payload) == node


@pytest.mark.parametrize("items", [[], [{"x": 1}, {"x": 1}]])
def test_omitted_cap_survives_payload_roundtrip(items) -> None:
    node = WorkflowNode.model_validate(declaration(items=items))
    payload = workflow_node_payload_dict(node)
    assert "max_items" not in payload
    assert WorkflowNode.model_validate(payload) == node


def test_payload_rejects_reference_with_missing_cap() -> None:
    node = WorkflowNode.model_validate(declaration(items="{{a.output}}", max_items=3))
    node.max_items = None
    with pytest.raises(ValueError, match="Incomplete foreach"):
        workflow_node_payload_dict(node)


@pytest.mark.parametrize("model", [WorkflowNodeConfig, WorkflowNode])
def test_explicit_cap_rejects_literal_overflow(model) -> None:
    with pytest.raises(ValueError, match="2 items exceeds max_items 1"):
        model.model_validate(declaration(items=[{}, {}], max_items=1))


@pytest.mark.parametrize("internal_parameters", [False, True])
def test_non_foreach_rejects_foreach_fields_in_sorted_order(
    internal_parameters: bool,
) -> None:
    value = {"id": "plain", "mode": "input", "workflow": "child.md", "items": []}
    original = copy.deepcopy(value)

    with pytest.raises(ValueError) as error:
        validate_foreach_declaration(value, internal_parameters)

    assert str(error.value) == "Fields only valid for foreach: items, workflow"
    assert value == original


@pytest.mark.parametrize(
    "overrides,message",
    [
        (
            {"with_params": {}, "providers": [], "workflow": ""},
            "Foreach 'group' cannot declare: providers, with_params",
        ),
        (
            {"workflow": "", "item_input": "", "max_items": 0},
            "Foreach 'group' requires a non-empty workflow",
        ),
        (
            {"workflow": "child.txt", "item_input": "", "max_items": 0},
            "Foreach 'group' requires a non-empty item_input",
        ),
        (
            {"workflow": "child.txt", "max_items": 0},
            "Foreach 'group' workflow must be a fixed .md path",
        ),
        (
            {"max_items": 0, "items": "invalid"},
            "Foreach 'group' max_items must be a strict positive integer",
        ),
        (
            {"items": [1], "inputs": None},
            "Foreach 'group' items[0]: expected an object",
        ),
        (
            {"inputs": None, "with": None},
            "Foreach 'group' inputs must be a string mapping",
        ),
        (
            {"inputs": {" shared ": "node", "shared": "INVALID"}},
            "Foreach 'group' duplicate normalized inputs key 'shared'",
        ),
        (
            {"inputs": {" shared ": "INVALID"}},
            "Foreach 'group' input ' shared ' must reference a caller node ID",
        ),
    ],
)
def test_validation_preserves_first_error_and_input(
    overrides: dict[str, object], message: str
) -> None:
    value = declaration(**overrides)
    original = copy.deepcopy(value)

    with pytest.raises(ValueError) as error:
        validate_foreach_declaration(value)

    assert str(error.value) == message
    assert value == original
