import json
from typing import cast

import pytest

from crewplane.architecture.contracts import (
    InvocationDiagnostic,
    LogLevel,
    RuntimeLogEventPayload,
)
from crewplane.observability.events import (
    event_from_record,
    execution_event_log_record,
    runtime_log_event,
)


@pytest.mark.parametrize("payload_type", [InvocationDiagnostic, RuntimeLogEventPayload])
@pytest.mark.parametrize("level", LogLevel)
def test_diagnostic_payloads_normalize_string_levels(
    payload_type: type[InvocationDiagnostic] | type[RuntimeLogEventPayload],
    level: LogLevel,
) -> None:
    payload = payload_type(
        level=cast(LogLevel, level.value),
        message="Retry scheduled",
        operation="retry",
    )

    assert payload.level is level


@pytest.mark.parametrize("payload_type", [InvocationDiagnostic, RuntimeLogEventPayload])
@pytest.mark.parametrize("level", ["verbose", "INFO", "", None, 1])
def test_diagnostic_payloads_reject_invalid_levels(
    payload_type: type[InvocationDiagnostic] | type[RuntimeLogEventPayload],
    level: object,
) -> None:
    with pytest.raises(ValueError):
        payload_type(
            level=cast(LogLevel, level), message="Retry scheduled", operation="retry"
        )


@pytest.mark.parametrize("level", LogLevel)
def test_runtime_log_round_trip_preserves_string_wire_values(level: LogLevel) -> None:
    event = runtime_log_event("workflow", "run", level, "Retry scheduled", "retry")
    record = execution_event_log_record(event)

    assert type(record["level"]) is str
    assert record["level"] == level.value
    restored = event_from_record(json.loads(json.dumps(record)))

    assert restored is not None
    assert isinstance(restored.payload, RuntimeLogEventPayload)
    assert restored.payload.level is level


@pytest.mark.parametrize("level", ["verbose", "INFO", "", None, 1, {}])
def test_runtime_log_reader_ignores_invalid_levels(level: object) -> None:
    event = runtime_log_event(
        "workflow", "run", LogLevel.INFO, "Retry scheduled", "retry"
    )
    record: dict[str, object] = dict(execution_event_log_record(event))
    record["level"] = level

    assert event_from_record(record) is None
