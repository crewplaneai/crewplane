import pytest

from crewplane.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
    expected_seeded_lineage_invocation,
    lineage_payload_order,
)
from crewplane.artifacts.workspace.state.lineage import (
    invocation_round_order,
    is_seeded_audit_round,
    review_output_coordinates,
)
from crewplane.core.workflow.keywords import ProviderRole


@pytest.mark.parametrize("audit_round_num", [None, 0, 1, 2])
@pytest.mark.parametrize("round_num", [0, 1, 2])
def test_seeded_round_requires_first_local_round_of_later_audit(
    round_num, audit_round_num
):
    seeded = (round_num, audit_round_num) == (1, 2)
    assert is_seeded_audit_round(round_num, audit_round_num) is seeded
    for role, required in [
        (ProviderRole.EXECUTOR, True),
        (ProviderRole.EXECUTOR, False),
        (ProviderRole.REVIEWER, True),
    ]:
        invocation = ExpectedWorkspaceInvocation(
            "task", role, round_num, audit_round_num, required
        )
        assert expected_seeded_lineage_invocation(invocation) is (
            seeded and role == ProviderRole.EXECUTOR and required
        )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"round_num": 1}, (0, 1)),
        ({"round_num": 0, "audit_round_num": None}, (0, 0)),
        ({"round_num": 2, "audit_round_num": 3}, (3, 2)),
        ({"round_num": -1, "audit_round_num": -2}, (-2, -1)),
        ({}, None),
        ({"round_num": None}, None),
        ({"round_num": True}, None),
        ({"round_num": "1"}, None),
        ({"round_num": 1, "audit_round_num": True}, None),
        ({"round_num": 1, "audit_round_num": "2"}, None),
    ],
)
def test_round_parsing_preserves_artifact_invalid_order(
    payload: dict[str, object], expected: tuple[int, int] | None
) -> None:
    assert invocation_round_order(payload) == expected
    lineage_payload = {
        **payload,
        "role": "executor",
        "workspace": {"lineage_producer": True},
    }
    assert lineage_payload_order(lineage_payload) == (
        expected if expected is not None else (-1, -1)
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("task_round2.md", (None, 2)),
        ("review-audit-round-3/task_round1.md", (3, 1)),
        ("unrecognized/task_round2.md", (None, 2)),
        ("other_round2.md", None),
        ("task_round0.md", None),
        ("task_round-2.md", None),
        ("task_round+2.md", (None, 2)),
        ("task_round 2 .md", (None, 2)),
        ("task_round1_round2.md", None),
        ("review-audit-round-0/task_round2.md", (None, 2)),
        ("review-audit-round--2/task_round2.md", (None, 2)),
        ("review-audit-round-+3/task_round2.md", (3, 2)),
        ("review-audit-round- 3 /task_round2.md", (3, 2)),
        ("task.md", None),
    ],
)
def test_review_output_coordinate_contract(
    path: str, expected: tuple[int | None, int] | None
) -> None:
    coordinates = review_output_coordinates(path, "task")
    if expected is None:
        assert coordinates is None
    else:
        assert coordinates is not None
        assert coordinates.task_id == "task"
        assert (coordinates.audit_round_num, coordinates.round_num) == expected
