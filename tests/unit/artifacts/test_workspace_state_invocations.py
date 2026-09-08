from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
    WorkspaceStateStatus,
    expected_failed_workspace_invocations,
    payload_matches_expected_invocation,
    workspace_state_payloads_for_status,
)
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.resume import make_plan
from tests.helpers.resume_validation import source_record


@pytest.mark.parametrize("continue_on_failure", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"task_id": None},
        {"task_id": "unknown"},
        {"provider": "unknown"},
        {"role": "executor"},
        {"round_num": True},
        {"audit_round_num": False},
    ],
)
def test_failed_reviewers_require_matching_provider_and_continuation_policy(
    tmp_path: Path,
    continue_on_failure: bool,
    changes: dict[str, object],
) -> None:
    source = source_record(tmp_path)
    node = make_plan(review_loop=True).nodes[0]
    node = node.model_copy(
        update={
            "execution_policy": node.execution_policy.model_copy(
                update={"continue_on_failure": continue_on_failure}
            )
        }
    )
    payload = {
        "status": "failed",
        "task_id": "beta",
        "provider": "beta",
        "role": "reviewer",
        "round_num": 0,
        "audit_round_num": None,
        **changes,
    }
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(parents=True)
    (stage_dir / "workspace-state-beta.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    expected = expected_failed_workspace_invocations(source, node)

    if continue_on_failure and not changes:
        assert expected == (
            ExpectedWorkspaceInvocation("beta", ProviderRole.REVIEWER, 0, None),
        )
    else:
        assert expected == ()


@pytest.mark.parametrize("continue_on_failure", [False, True])
@pytest.mark.parametrize("discarded", [False, True])
def test_failed_sequential_executor_requires_discarded_lineage(
    tmp_path: Path,
    continue_on_failure: bool,
    discarded: bool,
) -> None:
    source = source_record(tmp_path)
    node = make_plan(review_loop=True).nodes[0]
    node = node.model_copy(
        update={
            "execution_policy": node.execution_policy.model_copy(
                update={"continue_on_failure": continue_on_failure}
            )
        }
    )
    payload = {
        "status": "failed",
        "workspace_kind": "worktree",
        "task_id": "alpha",
        "provider": "alpha",
        "role": "executor",
        "round_num": 2,
        "audit_round_num": None,
        "result": {
            "lineage_produced": False,
            "lineage_discarded": discarded,
            "lineage_discard_reason": "remediation_context_exhausted",
        },
    }
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(parents=True)
    (stage_dir / "workspace-state-alpha-round2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    expected = expected_failed_workspace_invocations(source, node)

    assert expected == (
        (ExpectedWorkspaceInvocation("alpha", ProviderRole.EXECUTOR, 2, None),)
        if discarded
        else ()
    )


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
            role=ProviderRole.EXECUTOR,
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
            role=ProviderRole.EXECUTOR,
            round_num=1,
            audit_round_num=0,
        ),
    )
