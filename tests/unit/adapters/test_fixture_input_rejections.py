from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.adapters.invokers.mock_invoker.mutations import (
    build_fixture_mutation_plan,
)
from crewplane.adapters.invokers.mock_invoker.outputs import OutputResolution
from crewplane.adapters.invokers.mock_invoker.selectors import (
    selector_matches,
    validate_fail_selectors,
)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "list or object"),
        ({"unexpected": []}, "unsupported keys"),
        ({"mutations": "file"}, "must be a list"),
        ({"workspace_mutations": {}}, "must be a list"),
        ([None], "entries must be objects"),
        ([{"path": " ", "content": "text"}], "non-empty string"),
        ([{"path": "result.md", "content": None}], "content must be a string"),
        ([{"path": "../escape.md", "content": "text"}], "stay within"),
        ({"required_prompt_contains": "text"}, "must be a list"),
        ({"forbidden_prompt_contains": [None]}, "must be a list"),
        ({"required_prompt_contains": ["missing"]}, "did not contain required"),
        ({"forbidden_prompt_contains": ["secret"]}, "contained forbidden"),
        (
            {"workspace_mutations": [{"path": "new.md", "content": "text"}]},
            "require a workspace invocation context",
        ),
    ],
)
def test_invalid_fixture_sidecars_are_rejected_before_any_write(
    tmp_path: Path, payload: object, message: str
) -> None:
    fixture = tmp_path / "fixture.md"
    fixture.write_text("fixture", encoding="utf-8")
    fixture.with_suffix(".mutations.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    output = tmp_path / "output" / "response.md"
    resolution = OutputResolution("fixture", "fixture", fixture)

    with pytest.raises(RuntimeError, match=message):
        build_fixture_mutation_plan(resolution, output, tmp_path, None, "secret")

    assert not output.parent.exists()
    assert not (tmp_path / "escape.md").exists()
    assert fixture.read_text(encoding="utf-8") == "fixture"


@pytest.mark.parametrize("content", [b"{", b"\xff"])
def test_unreadable_fixture_sidecars_preserve_the_parse_error(
    tmp_path: Path, content: bytes
) -> None:
    fixture = tmp_path / "fixture.md"
    fixture.with_suffix(".mutations.json").write_bytes(content)

    with pytest.raises(RuntimeError, match="could not parse") as caught:
        build_fixture_mutation_plan(
            OutputResolution("fixture", "fixture", fixture),
            tmp_path / "output" / "response.md",
            tmp_path,
            None,
            "prompt",
        )

    assert isinstance(caught.value.__cause__, (ValueError, UnicodeDecodeError))
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "selector", [None, {"round_num": True}, {"audit_round_num": "1"}]
)
def test_failure_selectors_reject_non_objects_and_non_integer_rounds(
    selector: object,
) -> None:
    with pytest.raises(ValueError, match="objects|integer"):
        validate_fail_selectors([selector])


def test_context_free_invocations_do_not_match_failure_selectors() -> None:
    selector = validate_fail_selectors([{"node_id": "draft"}])[0]

    assert selector_matches(selector, None) is False


class IntegerSubclass(int):
    pass


@pytest.mark.parametrize("field", ["round_num", "audit_round_num"])
@pytest.mark.parametrize("value", [None, True, False, "1", 1.0])
def test_selector_integer_errors_preserve_exact_field(field, value):
    with pytest.raises(ValueError) as caught:
        validate_fail_selectors([{field: value}])
    assert str(caught.value) == (
        f"mock invoker option 'fail_when' selector key '{field}' must be an integer"
    )


@pytest.mark.parametrize("value", [0, -1, 2**80, IntegerSubclass(7)])
def test_selector_integer_values_are_not_coerced(value):
    selector = validate_fail_selectors([{"round_num": value}])[0]
    assert selector.round_num is value
    assert selector.audit_round_num is None


@pytest.mark.parametrize("first", ["audit_round_num", "round_num"])
def test_selector_errors_follow_authored_field_order(first):
    second = "round_num" if first == "audit_round_num" else "audit_round_num"
    with pytest.raises(ValueError) as caught:
        validate_fail_selectors([{first: True, second: False}])
    assert str(caught.value) == (
        f"mock invoker option 'fail_when' selector key '{first}' must be an integer"
    )
