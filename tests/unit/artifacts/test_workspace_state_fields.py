from __future__ import annotations

from orchestrator_cli.artifacts.workspace_state_fields import (
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
