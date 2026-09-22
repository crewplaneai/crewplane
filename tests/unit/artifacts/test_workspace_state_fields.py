from __future__ import annotations

import copy

import pytest

from crewplane.artifacts.workspace.state.fields import (
    encode_workspace_state_for_resume,
    int_field,
    nullable_int_field,
)


@pytest.mark.parametrize(
    ("payload", "expected", "nullable_valid"),
    [
        ({}, None, True),
        ({"value": None}, None, True),
        ({"value": True}, None, False),
        ({"value": False}, None, False),
        ({"value": "1"}, None, False),
        ({"value": 1.0}, None, False),
        ({"value": -1}, -1, True),
        ({"value": 0}, 0, True),
        ({"value": 1}, 1, True),
    ],
)
def test_integer_fields_preserve_missing_null_and_strict_integer_semantics(
    payload: dict[str, object], expected: int | None, nullable_valid: bool
) -> None:
    assert int_field(payload, "value") == expected
    nullable = nullable_int_field(payload, "value")
    assert nullable.value == expected
    assert nullable.valid is nullable_valid


def test_resume_encoding_preserves_exact_bytes_without_mutating_state() -> None:
    state = {
        "z": "café",
        "branch_export": {"status": "pending"},
        "a": [{"branch_export": {"status": "succeeded"}, "value": 1}],
    }
    original = copy.deepcopy(state)

    encoded = encode_workspace_state_for_resume(state)

    assert encoded == b'{"a":[{"value":1}],"z":"caf\\u00e9"}'
    assert state == original
    state["branch_export"] = {"status": "failed"}
    assert encode_workspace_state_for_resume(state) == encoded


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_resume_encoding_rejects_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        encode_workspace_state_for_resume({"value": value})
