from __future__ import annotations

from orchestrator_cli.artifacts.workspace.source_validation import (
    workspace_invocation_source_matches,
)
from tests.helpers.resume import make_plan
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    provider_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import workspace_selection_record


def test_workspace_invocation_source_rejects_bool_round_num(tmp_path) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    policy = workspace_selection_record(
        enabled=True,
        kind="worktree",
        clean_start="strict",
        materialization="worktree_checkout",
    )
    node = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    assert plan.workspace_source is not None
    payload = provider_workspace_state_payload(
        source,
        plan,
        source_commit=plan.workspace_source.run_base_commit,
        source_tree=plan.workspace_source.source_tree,
    )
    payload["round_num"] = True

    assert not workspace_invocation_source_matches(source, plan, node, payload)
