"""Decode invocation identity from persisted workspace evidence."""

from collections.abc import Mapping

from crewplane.core.workspace.invocation_identity import invocation_slug


def state_invocation_slug(payload: Mapping[str, object]) -> str | None:
    node_id = payload.get("node_id")
    task_id = payload.get("task_id")
    round_num = payload.get("round_num")
    audit_round_num = payload.get("audit_round_num")
    if not (
        isinstance(node_id, str)
        and node_id
        and isinstance(task_id, str)
        and task_id
        and isinstance(round_num, int)
        and not isinstance(round_num, bool)
        and (
            audit_round_num is None
            or isinstance(audit_round_num, int)
            and not isinstance(audit_round_num, bool)
        )
    ):
        return None
    return invocation_slug(node_id, task_id, audit_round_num, round_num)
