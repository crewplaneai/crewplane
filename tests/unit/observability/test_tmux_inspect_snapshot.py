import json
from pathlib import Path

import pytest

from crewplane.observability.tmux.inspect_snapshot import (
    read_inspect_snapshot,
    read_snapshot,
)


def test_read_snapshot_accepts_complete_selected_invocation_snapshot(
    tmp_path: Path,
) -> None:
    path = write_snapshot(tmp_path, selected_snapshot())

    assert read_snapshot(path) == selected_snapshot()


def test_read_snapshot_accepts_complete_inspect_invocation_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = inspect_snapshot()

    assert read_snapshot(write_snapshot(tmp_path, snapshot)) == snapshot


def test_read_inspect_snapshot_rejects_snapshot_without_inspect_fields(
    tmp_path: Path,
) -> None:
    path = write_snapshot(tmp_path, selected_snapshot())

    assert read_snapshot(path) == selected_snapshot()
    assert read_inspect_snapshot(path) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("workflow_name", 42, id="wrong-required-type"),
        pytest.param("selection_generation", "zero", id="wrong-integer-type"),
        pytest.param("written_at", 0, id="wrong-float-type"),
        pytest.param("invocation_status", "cancelled", id="wrong-optional-type"),
    ],
)
def test_read_snapshot_rejects_invalid_field_types(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    snapshot = selected_snapshot()
    snapshot[field] = value

    assert read_snapshot(write_snapshot(tmp_path, snapshot)) is None


@pytest.mark.parametrize("field", ["inspect_view", "line_budget", "created_at"])
def test_read_snapshot_rejects_missing_inspect_field(
    tmp_path: Path,
    field: str,
) -> None:
    snapshot = inspect_snapshot()
    del snapshot[field]

    assert read_snapshot(write_snapshot(tmp_path, snapshot)) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("inspect_view", "compact", id="unsupported-view"),
        pytest.param("inspect_view", 42, id="wrong-view-type"),
        pytest.param("line_budget", "20", id="wrong-line-budget-type"),
        pytest.param("line_budget", True, id="boolean-line-budget"),
        pytest.param("created_at", "now", id="wrong-created-at-type"),
        pytest.param("created_at", 0, id="integer-created-at"),
    ],
)
def test_read_snapshot_rejects_invalid_inspect_field(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    snapshot = inspect_snapshot()
    snapshot[field] = value

    assert read_snapshot(write_snapshot(tmp_path, snapshot)) is None


def test_read_snapshot_rejects_missing_required_field(tmp_path: Path) -> None:
    snapshot = selected_snapshot()
    del snapshot["run_id"]

    assert read_snapshot(write_snapshot(tmp_path, snapshot)) is None


def write_snapshot(tmp_path: Path, snapshot: dict[str, object]) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def selected_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_name": "workflow",
        "run_id": "run",
        "dashboard_generation": 1,
        "selection_generation": 0,
        "requested_selected_index": -1,
        "resolved_selected_index": 0,
        "node_count": 1,
        "node_id": "node.a",
        "written_at": 0.0,
        "log_file": "/tmp/provider.log",
    }


def inspect_snapshot() -> dict[str, object]:
    snapshot = selected_snapshot()
    snapshot.update(
        {
            "inspect_view": "formatted",
            "line_budget": 20,
            "created_at": 0.0,
        }
    )
    return snapshot
