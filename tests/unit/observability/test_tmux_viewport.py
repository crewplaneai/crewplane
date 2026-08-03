import pytest

from crewplane.observability.tmux.viewport import viewport_dag_lines


@pytest.mark.parametrize(
    ("lines", "height", "selected", "expected"),
    [
        pytest.param(["a"], 0, 0, [], id="zero-height"),
        pytest.param([], 3, None, [], id="empty-lines"),
        pytest.param(["a", "b"], 3, 1, ["a", "b"], id="all-fit"),
        pytest.param(["a", "b", "c"], 1, None, ["a"], id="one-row-default"),
        pytest.param(["a", "b", "c"], 1, -1, ["a"], id="negative-selection"),
        pytest.param(["a", "b", "c"], 1, 9, ["a"], id="selection-too-large"),
        pytest.param(["a", "b", "c"], 1, 2, ["c"], id="one-row-selected"),
        pytest.param(
            ["a", "b", "c"],
            2,
            0,
            ["a", "... below ..."],
            id="two-rows-start",
        ),
        pytest.param(
            ["a", "b", "c"],
            2,
            2,
            ["... above ...", "c"],
            id="two-rows-end",
        ),
        pytest.param(
            ["a", "b", "c", "d", "e"],
            2,
            2,
            ["c", "... below ..."],
            id="two-rows-middle-tie",
        ),
        pytest.param(
            ["a", "b", "c", "d", "e", "f"],
            2,
            4,
            ["... above ...", "e"],
            id="two-rows-middle-above",
        ),
        pytest.param(
            ["a", "b", "c", "d", "e"],
            3,
            2,
            ["... above ...", "c", "... below ..."],
            id="standard-middle",
        ),
        pytest.param(
            ["a", "b", "c", "d", "e"],
            4,
            0,
            ["a", "b", "c", "... below ..."],
            id="standard-expands-down",
        ),
        pytest.param(
            ["a", "b", "c", "d", "e"],
            4,
            4,
            ["... above ...", "c", "d", "e"],
            id="standard-expands-up",
        ),
    ],
)
def test_viewport_dag_lines(
    lines: list[str],
    height: int,
    selected: int | None,
    expected: list[str],
) -> None:
    assert viewport_dag_lines(lines, height, selected) == expected
