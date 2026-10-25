from itertools import permutations

import pytest

from crewplane.artifacts.workspace.state.lineage import (
    unique_latest_lineage_payload,
)


def test_unique_latest_lineage_has_no_selection_for_empty_input() -> None:
    assert unique_latest_lineage_payload([]) is None


def test_unique_latest_lineage_returns_single_payload_unchanged() -> None:
    payload: dict[str, object] = {"result": {"commit": "single"}}

    assert unique_latest_lineage_payload([((0, 1), payload)]) is payload


@pytest.mark.parametrize("permutation", list(permutations(range(3))))
def test_unique_latest_lineage_uses_greatest_order_in_every_permutation(
    permutation: tuple[int, ...],
) -> None:
    first: dict[str, object] = {"commit": "first"}
    second: dict[str, object] = {"commit": "second"}
    latest: dict[str, object] = {"commit": "latest"}
    ordered = [((0, 1), first), ((0, 2), second), ((1, 0), latest)]

    assert (
        unique_latest_lineage_payload([ordered[index] for index in permutation])
        is latest
    )


@pytest.mark.parametrize("permutation", list(permutations(range(3))))
@pytest.mark.parametrize("same_payload", [False, True])
def test_unique_latest_lineage_rejects_equal_latest_records_in_every_permutation(
    permutation: tuple[int, ...], same_payload: bool
) -> None:
    older: dict[str, object] = {"commit": "older"}
    latest: dict[str, object] = {"commit": "latest"}
    duplicate = latest if same_payload else dict(latest)
    ordered = [((0, 1), older), ((0, 2), latest), ((0, 2), duplicate)]

    assert (
        unique_latest_lineage_payload([ordered[index] for index in permutation]) is None
    )


def test_unique_latest_lineage_allows_ties_below_the_greatest_order() -> None:
    older: dict[str, object] = {"commit": "older"}
    latest: dict[str, object] = {"commit": "latest"}

    assert (
        unique_latest_lineage_payload(
            [((0, 1), older), ((0, 2), latest), ((0, 1), dict(older))]
        )
        is latest
    )
