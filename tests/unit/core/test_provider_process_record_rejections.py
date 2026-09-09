from __future__ import annotations

import pytest
from pydantic import ValidationError

from crewplane.core.execution_state import RUN_STATE_SCHEMA_VERSION
from crewplane.core.provider_process_state import ProviderProcessState
from crewplane.core.workflow.keywords import ProviderRole


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"run_state_schema_version": 0}, "Unsupported run state schema version"),
        ({"run_id": " "}, "identity fields must not be empty"),
        ({"run_key_name": " "}, "identity fields must not be empty"),
        ({"node_id": " "}, "identity fields must not be empty"),
        ({"task_id": " "}, "identity fields must not be empty"),
        ({"provider": " "}, "identity fields must not be empty"),
        ({"hostname": " "}, "identity fields must not be empty"),
        (
            {"returncode": 0},
            "Started provider process records cannot contain exit fields",
        ),
        (
            {"exited_at": "2026-01-01T00:00:01Z"},
            "Started provider process records cannot contain exit fields",
        ),
        ({"status": "exited", "returncode": 0}, "require exit time and return code"),
        (
            {"status": "exited", "exited_at": "2026-01-01T00:00:01Z"},
            "require exit time and return code",
        ),
    ],
)
def test_provider_process_record_rejects_invalid_identity_and_lifecycle_fields(
    changes: dict[str, object], message: str
) -> None:
    state = ProviderProcessState(
        run_state_schema_version=RUN_STATE_SCHEMA_VERSION,
        run_id="run",
        run_key_name="workflow--run",
        node_id="review",
        task_id="codex",
        provider="codex",
        role=ProviderRole.EXECUTOR,
        round_num=0,
        attempt=1,
        pid=123,
        hostname="test-host",
        status="started",
        started_at="2026-01-01T00:00:00Z",
    )
    payload = state.model_dump(mode="json")
    payload.update(changes)

    with pytest.raises(ValidationError, match=message):
        ProviderProcessState.model_validate(payload)
