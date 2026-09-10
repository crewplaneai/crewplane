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
