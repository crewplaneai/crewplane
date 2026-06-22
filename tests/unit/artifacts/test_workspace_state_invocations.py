from __future__ import annotations

import json

from orchestrator_cli.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
    WorkspaceStateStatus,
    payload_matches_expected_invocation,
    workspace_state_payloads_for_status,
)
from tests.helpers.resume import make_plan
from tests.helpers.resume_validation import source_record


def test_workspace_state_payloads_for_status_filters_by_str_enum(tmp_path) -> None:
    source = source_record(tmp_path)
    node = make_plan().nodes[0]
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(parents=True)
    (stage_dir / "workspace-state-alpha.json").write_text(
        json.dumps({"status": "succeeded", "task_id": "alpha"}),
        encoding="utf-8",
    )
    (stage_dir / "workspace-state-beta.json").write_text(
        json.dumps({"status": "failed", "task_id": "beta"}),
        encoding="utf-8",
    )

    succeeded = workspace_state_payloads_for_status(
        source,
        node,
        WorkspaceStateStatus.SUCCEEDED,
    )
    failed = workspace_state_payloads_for_status(
        source,
        node,
        WorkspaceStateStatus.FAILED,
    )

    assert [payload["task_id"] for payload in succeeded] == ["alpha"]
    assert [payload["task_id"] for payload in failed] == ["beta"]


def test_payload_matches_expected_invocation_rejects_bool_round_num() -> None:
    assert not payload_matches_expected_invocation(
        {
            "task_id": "alpha",
            "role": "executor",
            "round_num": True,
            "audit_round_num": None,
        },
        ExpectedWorkspaceInvocation(
            task_id="alpha",
            role="executor",
            round_num=1,
            audit_round_num=None,
        ),
    )


def test_payload_matches_expected_invocation_rejects_bool_audit_round_num() -> None:
    assert not payload_matches_expected_invocation(
        {
            "task_id": "alpha",
            "role": "executor",
            "round_num": 1,
            "audit_round_num": False,
        },
        ExpectedWorkspaceInvocation(
            task_id="alpha",
            role="executor",
            round_num=1,
            audit_round_num=0,
        ),
    )
