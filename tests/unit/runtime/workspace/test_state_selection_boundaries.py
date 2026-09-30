from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from crewplane.architecture.contracts import NodeArtifactRequest
from crewplane.artifacts.workspace.state.lineage import ReviewOutputCoordinates
from crewplane.runtime.workspace.state_selection import (
    find_lineage_state_path,
    iter_lineage_states,
    latest_executor_lineage_state_path,
    read_workspace_state,
    required_lineage_state_path,
    same_node_executor_state_path,
    workspace_state_is_lineage_source,
)
from tests.helpers.resume import make_plan
from tests.unit.runtime.workspace.state_selection_support import (
    ArtifactStore,
    same_selection_node,
    write_selection_state,
)


def lineage_payload(round_num: object = 1, task_id: str = "alpha") -> dict[str, object]:
    return {
        "task_id": task_id,
        "round_num": round_num,
        "audit_round_num": None,
        "role": "executor",
        "status": "succeeded",
        "workspace": {"lineage_producer": True},
        "result": {"result_commit": "a" * 40, "result_tree": "b" * 40},
    }


@pytest.mark.parametrize("payload", [b"{broken", b"\xff", b"[]", b"null"])
def test_invalid_state_files_are_not_selected_as_lineage_sources(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "workspace-state.json"
    path.write_bytes(payload)
    assert read_workspace_state(path) == {}
    assert not workspace_state_is_lineage_source(path)
    assert latest_executor_lineage_state_path(tmp_path) is None


def test_missing_state_is_not_a_lineage_source(tmp_path: Path) -> None:
    path = tmp_path / "workspace-state.json"
    assert read_workspace_state(path) == {}
    assert not workspace_state_is_lineage_source(path)


@pytest.mark.parametrize("round_num", [None, "1", True])
def test_lineage_selection_skips_invalid_round_coordinates(
    tmp_path: Path, round_num: object
) -> None:
    path = tmp_path / "workspace-state.json"
    path.write_text(json.dumps(lineage_payload(round_num)), encoding="utf-8")
    assert latest_executor_lineage_state_path(tmp_path) is None


def test_lineage_selection_filters_task_and_before_boundary(tmp_path: Path) -> None:
    for task, round_num in [("alpha", 1), ("alpha", 2), ("beta", 3)]:
        path = tmp_path / f"workspace-state-{task}-round{round_num}.json"
        path.write_text(json.dumps(lineage_payload(round_num, task)), encoding="utf-8")
    selected = iter_lineage_states(tmp_path, {"alpha"}, before=(0, 2))
    assert [(order, path.name) for order, path, _ in selected] == [
        ((0, 1), "workspace-state-alpha-round1.json")
    ]
    assert (
        latest_executor_lineage_state_path(tmp_path, {"alpha"}, before=(0, 1)) is None
    )


@pytest.mark.parametrize("audit_round_num", [None, 0, 1, 2])
@pytest.mark.parametrize("round_num", [0, 1, 2])
def test_same_node_lineage_seeded_round_boundary(tmp_path, round_num, audit_round_num):
    previous = tmp_path / "implement" / "workspace-state-prior.json"
    write_selection_state(previous, "1" * 40, round_num=2, audit_round_num=1)
    expected = previous if (audit_round_num, round_num) in {(1, 2), (2, 1)} else None
    assert (
        same_node_executor_state_path(
            ArtifactStore(tmp_path), same_selection_node(), round_num, audit_round_num
        )
        == expected
    )


@pytest.mark.parametrize("audit_round_num", [None, 0])
def test_same_node_lineage_keeps_null_and_zero_audit_identity(
    tmp_path, audit_round_num
):
    stage_dir = tmp_path / "implement"
    for audit in (None, 0):
        write_selection_state(
            stage_dir / f"workspace-state-{audit}.json",
            "1" * 40,
            round_num=0,
            audit_round_num=audit,
        )
    assert (
        same_node_executor_state_path(
            ArtifactStore(tmp_path), same_selection_node(), 0, audit_round_num
        )
        == stage_dir / f"workspace-state-{audit_round_num}.json"
    )


def test_same_node_seeded_lineage_prefers_exact_state(tmp_path):
    stage_dir = tmp_path / "implement"
    previous = stage_dir / "workspace-state-prior.json"
    exact = stage_dir / "workspace-state-exact.json"
    write_selection_state(previous, "1" * 40, round_num=2, audit_round_num=1)
    write_selection_state(exact, "2" * 40, round_num=1, audit_round_num=2)
    assert (
        same_node_executor_state_path(
            ArtifactStore(tmp_path), same_selection_node(), 1, 2
        )
        == exact
    )


@pytest.mark.parametrize("audit_round_num", [True, "2", 2.0])
def test_lineage_selection_skips_invalid_audit_coordinates(tmp_path, audit_round_num):
    payload = {**lineage_payload(), "audit_round_num": audit_round_num}
    (tmp_path / "workspace-state.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    assert latest_executor_lineage_state_path(tmp_path) is None


@pytest.mark.parametrize("exact", [False, True])
def test_lineage_selection_rejects_duplicate_invocation_candidates(
    tmp_path: Path, exact: bool
) -> None:
    for name in ["workspace-state.json", "workspace-state-copy.json"]:
        (tmp_path / name).write_text(json.dumps(lineage_payload()), encoding="utf-8")
    lookup = (
        partial(
            find_lineage_state_path,
            tmp_path,
            ReviewOutputCoordinates("alpha", 1, None),
        )
        if exact
        else partial(latest_executor_lineage_state_path, tmp_path)
    )
    with pytest.raises(
        RuntimeError, match="Ambiguous executor workspace states"
    ) as raised:
        lookup()
    assert "workspace-state-copy.json" in str(raised.value)
    assert "workspace-state.json" in str(raised.value)


class StageLookup:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def get_node_dir(self, request: NodeArtifactRequest) -> Path | None:
        assert request.node_id == "a"
        return self.path


@pytest.mark.parametrize("missing_stage", [False, True])
def test_required_lineage_source_reports_missing_artifacts(
    tmp_path: Path, missing_stage: bool
) -> None:
    output = StageLookup(None if missing_stage else tmp_path)
    with pytest.raises(
        RuntimeError, match="no stage directory|no succeeded workspace state"
    ):
        required_lineage_state_path(output, make_plan().nodes[0])
