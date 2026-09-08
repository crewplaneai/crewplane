from __future__ import annotations

import copy

import pytest

from crewplane.artifacts.workspace.state.fields import (
    encode_workspace_state_for_resume,
    int_field,
    nullable_int_field,
)


def test_int_field_rejects_bool_and_non_int_values() -> None:
    assert int_field({"round_num": 1}, "round_num") == 1
    assert int_field({"round_num": True}, "round_num") is None
    assert int_field({"round_num": "1"}, "round_num") is None
    assert int_field({}, "round_num") is None


def test_nullable_int_field_distinguishes_null_from_invalid_values() -> None:
    null_field = nullable_int_field({"audit_round_num": None}, "audit_round_num")
    missing_field = nullable_int_field({}, "audit_round_num")
    valid_field = nullable_int_field({"audit_round_num": 2}, "audit_round_num")
    bool_field = nullable_int_field({"audit_round_num": False}, "audit_round_num")
    text_field = nullable_int_field({"audit_round_num": "2"}, "audit_round_num")

    assert null_field.valid is True
    assert null_field.value is None
    assert missing_field.valid is True
    assert missing_field.value is None
    assert valid_field.valid is True
    assert valid_field.value == 2
    assert bool_field.valid is False
    assert text_field.valid is False


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
