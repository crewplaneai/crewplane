import copy

import pytest

from crewplane.core.workflow.validation.foreach import validate_foreach_declaration


def declaration(**overrides: object) -> dict[str, object]:
    return {
        "id": "group",
        "mode": "foreach",
        "workflow": "child.md",
        "item_input": "item",
        "items": [],
        "max_items": 3,
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

    result = validate_foreach_declaration(declaration(items=reference))

    assert isinstance(result, dict)
    assert result["items"] is reference


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
