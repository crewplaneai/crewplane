from __future__ import annotations

from pathlib import Path

import pytest

from crewplane.core.preflight.serialization import canonical_json_bytes


def test_canonical_json_normalizes_nested_paths_and_tuples() -> None:
    payload = {
        "files": (Path("docs/context.md"), {"path": Path("input.md")}),
        "optional": None,
    }

    assert (
        canonical_json_bytes(payload)
        == b'{"files":["docs/context.md",{"path":"input.md"}],"optional":null}'
    )


@pytest.mark.parametrize("value", [object(), {"unsupported": object()}, [object()]])
def test_canonical_json_rejects_values_without_a_stable_json_representation(
    value: object,
) -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        canonical_json_bytes(value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_json_bytes({"value": value})
