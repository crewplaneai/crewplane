from __future__ import annotations

from unittest.mock import patch

import pytest

from crewplane.observability.events.inspection import inspection_event_problem
from crewplane.observability.events.reader import event_from_record


@pytest.fixture
def invocation_record() -> dict[str, object]:
    return {
        "event_type": "invocation_started",
        "workflow_name": "workflow",
        "run_id": "run-1",
        "timestamp": "2026-08-04T00:00:00+00:00",
        "node_id": "node",
        "task_id": "task",
        "provider": "mock",
        "round_num": 0,
    }


@pytest.mark.parametrize(
    "changes, expected",
    [
        (
            {"timestamp": "invalid", "log_file": 42, "node_id": ""},
            "timestamp",
        ),
        ({"log_file": 42, "node_id": ""}, "invocation log reference"),
        ({"node_id": "", "round_num": True}, "invocation coordinates"),
        ({"log_file": "node/invocation.log", "audit_round_num": 1}, None),
        (
            {"event_type": "workflow_finished", "log_file": 42, "round_num": -1},
            None,
        ),
        (
            {"event_type": "workflow_finished", "timestamp": "invalid"},
            "timestamp",
        ),
    ],
)
def test_validation_preserves_first_error_and_event_scope(
    invocation_record: dict[str, object],
    changes: dict[str, object],
    expected: str | None,
) -> None:
    invocation_record.update(changes)
    event = event_from_record(invocation_record)
    assert event is not None

    assert inspection_event_problem(event, invocation_record) == expected


@pytest.mark.parametrize("operation", ["fromisoformat", "timestamp"])
@pytest.mark.parametrize("error_type", [ValueError, OverflowError, OSError, TypeError])
def test_timestamp_conversion_exception_boundary(
    invocation_record: dict[str, object],
    operation: str,
    error_type: type[Exception],
) -> None:
    event = event_from_record(invocation_record)
    assert event is not None

    with patch("crewplane.observability.events.inspection.datetime") as parser:
        conversion = parser.fromisoformat
        if operation == "timestamp":
            conversion = conversion.return_value.timestamp
        conversion.side_effect = error_type("timestamp conversion failed")

        if error_type is TypeError:
            with pytest.raises(TypeError, match="timestamp conversion failed"):
                inspection_event_problem(event, invocation_record)
        else:
            assert inspection_event_problem(event, invocation_record) == "timestamp"
