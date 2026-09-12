"""Decode invocation identity from persisted workspace evidence."""

from collections.abc import Mapping
from typing import Literal

from crewplane.core.workspace.invocation_identity import invocation_slug


def state_invocation_slug(payload: Mapping[str, object]) -> str | None:
    try:
        return require_state_invocation_slug(payload)
    except InvalidInvocationField:
        return None


class InvalidInvocationField(ValueError):
    """Identify the first invalid persisted invocation field."""

    def __init__(
        self, field: Literal["node_id", "task_id", "round_num", "audit_round_num"]
    ) -> None:
        self.field = field
        super().__init__(field)


def require_state_invocation_slug(payload: Mapping[str, object]) -> str:
    node_id = payload.get("node_id")
    task_id = payload.get("task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if not isinstance(node_id, str) or not node_id:
        raise InvalidInvocationField("node_id")
    if not isinstance(task_id, str) or not task_id:
        raise InvalidInvocationField("task_id")
    if not isinstance(round_num, int) or isinstance(round_num, bool):
        raise InvalidInvocationField("round_num")
    if audit_round_num is not None and (
        not isinstance(audit_round_num, int) or isinstance(audit_round_num, bool)
    ):
        raise InvalidInvocationField("audit_round_num")
    return invocation_slug(node_id, task_id, audit_round_num, round_num)
