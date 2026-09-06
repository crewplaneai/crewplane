from __future__ import annotations


def apply_lineage_discard(payload: dict[str, object], reason: str) -> None:
    workspace = payload.get("workspace")
    if isinstance(workspace, dict):
        workspace["lineage_producer"] = False
    payload["result"] = _discarded_result(payload.get("result"), reason)
    payload.pop("refs", None)
    payload.pop("bundle", None)
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
    diagnostics.append(
        {
            "level": "warning",
            "message": f"Workspace lineage discarded: {reason}",
        }
    )
    payload["diagnostics"] = diagnostics


def _discarded_result(result: object, reason: str) -> dict[str, object]:
    discarded: dict[str, object] = {
        "lineage_produced": False,
        "lineage_discarded": True,
        "lineage_discard_reason": reason,
    }
    if not isinstance(result, dict):
        return discarded
    changed_path_count = result.get("changed_path_count")
    if isinstance(changed_path_count, int) and not isinstance(changed_path_count, bool):
        discarded["changed_path_count"] = changed_path_count
    final_head = result.get("final_head")
    if final_head is None:
        final_head = result.get("result_commit") or result.get("candidate_commit")
    if isinstance(final_head, str):
        discarded["final_head"] = final_head
    return discarded
