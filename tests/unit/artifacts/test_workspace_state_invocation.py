from types import MappingProxyType

import pytest

from crewplane.artifacts.workspace.state.invocation import state_invocation_slug
from crewplane.core.workspace.invocation_identity import invocation_slug

MISSING = object()


@pytest.mark.parametrize(
    "field", ["node_id", "task_id", "round_num", "audit_round_num"]
)
@pytest.mark.parametrize(
    "value", [MISSING, None, "", " ", "name", 0, -1, 12, True, False, 1.0, [], {}]
)
def test_persisted_invocation_identity_boundary(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "node_id": "node",
        "task_id": "task",
        "round_num": 1,
        "audit_round_num": None,
    }
    if value is MISSING:
        del payload[field]
    else:
        payload[field] = value
    if field in {"node_id", "task_id"}:
        valid = isinstance(value, str) and bool(value)
    else:
        valid = isinstance(value, int) and not isinstance(value, bool)
        if field == "audit_round_num" and (value is None or value is MISSING):
            valid = True

    result = state_invocation_slug(MappingProxyType(payload))

    if valid:
        assert result == invocation_slug(
            payload["node_id"],
            payload["task_id"],
            payload.get("audit_round_num"),
            payload["round_num"],
        )
    else:
        assert result is None


@pytest.mark.parametrize(
    ("node_id", "task_id"),
    [(" node ", " task "), ("node" * 100, "task" * 100), ("é/節点", "task_round0")],
)
def test_persisted_invocation_identity_preserves_exact_slug(
    node_id: str, task_id: str
) -> None:
    payload = {
        "node_id": node_id,
        "task_id": task_id,
        "round_num": -1,
        "audit_round_num": 0,
    }

    assert state_invocation_slug(payload) == invocation_slug(node_id, task_id, 0, -1)
