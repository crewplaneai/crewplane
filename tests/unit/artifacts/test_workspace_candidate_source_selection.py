from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from crewplane.artifacts.workspace.state.expected_set import (
    workspace_state_payloads_match_expected_set,
)
from crewplane.artifacts.workspace.state.invocations import (
    ExpectedWorkspaceInvocation,
)
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.resume import make_plan
from tests.helpers.resume_validation import source_record


@pytest.mark.parametrize("audit_round_num", [None, 1, 2])
def test_candidate_source_can_skip_discarded_executor_round(
    tmp_path: Path,
    audit_round_num: int | None,
) -> None:
    first = _lineage_payload(1, audit_round_num)
    discarded = _lineage_payload(2, audit_round_num, first)
    discarded["workspace"] = {"lineage_producer": False}
    discarded["result"] = {"lineage_produced": False, "lineage_discarded": True}
    third = _lineage_payload(3, audit_round_num, first)
    payloads = (first, discarded, third)

    assert _source_matches(tmp_path, payloads, third)
    assert workspace_state_payloads_match_expected_set(payloads, (_expected(third),))


def test_candidate_source_rejects_older_matching_result(tmp_path: Path) -> None:
    first = _lineage_payload(1)
    second = _lineage_payload(2, source=first)
    third = _lineage_payload(3, source=first)
    payloads = (first, second, third)

    assert not _source_matches(tmp_path, payloads, third)
    assert not workspace_state_payloads_match_expected_set(
        payloads, (_expected(third),)
    )


@pytest.mark.parametrize("role", [ProviderRole.EXECUTOR, ProviderRole.REVIEWER])
def test_candidate_source_rejects_ambiguous_latest_result(
    tmp_path: Path, role: ProviderRole
) -> None:
    first = _lineage_payload(1)
    duplicate = deepcopy(first)
    current = (
        _lineage_payload(3, source=first)
        if role == ProviderRole.EXECUTOR
        else _reviewer_payload(1, None, first)
    )
    payloads = (first, duplicate, current)

    assert not _source_matches(tmp_path, payloads, current)
    assert not workspace_state_payloads_match_expected_set(
        payloads, (_expected(current),)
    )


@pytest.mark.parametrize("field", ["commit", "tree", "bundle_sha256", "bundle_ref"])
def test_candidate_source_requires_exact_latest_descriptor(
    tmp_path: Path,
    field: str,
) -> None:
    first = _lineage_payload(1)
    third = _lineage_payload(3, source=first)
    descriptor = third["source"]
    assert isinstance(descriptor, dict)
    descriptor[field] = "mismatch"
    _set_invocation_source(third)
    payloads = (first, third)

    assert not _source_matches(tmp_path, payloads, third)
    assert not workspace_state_payloads_match_expected_set(
        payloads, (_expected(third),)
    )


def test_reviewer_candidate_source_requires_same_round(tmp_path: Path) -> None:
    first = _lineage_payload(1)
    reviewer = _reviewer_payload(3, None, first)
    payloads = (first, reviewer)

    assert not _source_matches(tmp_path, payloads, reviewer)
    assert not workspace_state_payloads_match_expected_set(
        payloads, (_expected(reviewer),)
    )


def test_seeded_reviewer_uses_latest_prior_audit_candidate(tmp_path: Path) -> None:
    first = _lineage_payload(1, 1)
    second = _lineage_payload(2, 1, first)
    reviewer = _reviewer_payload(1, 2, second)
    payloads = (first, second, reviewer)

    assert _source_matches(tmp_path, payloads, reviewer)
    assert workspace_state_payloads_match_expected_set(payloads, (_expected(reviewer),))


def test_seeded_reviewer_rejects_older_matching_candidate(tmp_path: Path) -> None:
    first = _lineage_payload(1, 1)
    second = _lineage_payload(2, 1)
    reviewer = _reviewer_payload(1, 2, first)
    payloads = (first, second, reviewer)

    assert not _source_matches(tmp_path, payloads, reviewer)
    assert not workspace_state_payloads_match_expected_set(
        payloads, (_expected(second), _expected(reviewer))
    )


def _source_matches(
    tmp_path: Path,
    payloads: tuple[dict[str, object], ...],
    payload: dict[str, object],
) -> bool:
    from crewplane.artifacts.workspace.source_validation import (
        workspace_invocation_source_matches,
    )

    run = source_record(tmp_path)
    plan = make_plan()
    stage_dir = run.run_dir / "a"
    stage_dir.mkdir(parents=True)
    for index, candidate in enumerate(payloads):
        (stage_dir / f"workspace-state-{index}.json").write_text(
            json.dumps(candidate), encoding="utf-8"
        )
    return workspace_invocation_source_matches(run, plan, plan.nodes[0], payload)


def _expected(payload: dict[str, object]) -> ExpectedWorkspaceInvocation:
    return ExpectedWorkspaceInvocation(
        task_id=str(payload["task_id"]),
        role=ProviderRole(str(payload["role"])),
        round_num=int(str(payload["round_num"])),
        audit_round_num=payload["audit_round_num"],
    )


def _reviewer_payload(
    round_num: int,
    audit_round_num: int | None,
    source: dict[str, object],
) -> dict[str, object]:
    reviewer = _lineage_payload(round_num, audit_round_num, source)
    reviewer["task_id"] = "reviewer"
    reviewer["role"] = "reviewer"
    reviewer["workspace"] = {"lineage_producer": False}
    reviewer["result"] = {"lineage_produced": False}
    return reviewer


def _lineage_payload(
    round_num: int,
    audit_round_num: int | None = None,
    source: dict[str, object] | None = None,
) -> dict[str, object]:
    slug = f"audit-{audit_round_num}-round-{round_num}"
    payload: dict[str, object] = {
        "node_id": "a",
        "task_id": "executor",
        "role": "executor",
        "status": "succeeded",
        "round_num": round_num,
        "audit_round_num": audit_round_num,
        "workspace": {"lineage_producer": True},
        "result": {"result_commit": f"{slug}-commit", "result_tree": f"{slug}-tree"},
        "bundle": {
            "path": f"a/workspace-bundles/{slug}.bundle",
            "sha256": str(round_num) * 64,
            "size_bytes": round_num,
        },
        "refs": {"result": f"refs/crewplane/result/{slug}"},
    }
    if source is not None:
        result, bundle, refs = source["result"], source["bundle"], source["refs"]
        assert isinstance(result, dict)
        assert isinstance(bundle, dict)
        assert isinstance(refs, dict)
        payload["source"] = {
            "kind": "candidate",
            "node_id": "a",
            "commit": result["result_commit"],
            "tree": result["result_tree"],
            "candidate_sequence": 1,
            "bundle_path": bundle["path"],
            "bundle_sha256": bundle["sha256"],
            "bundle_size_bytes": bundle["size_bytes"],
            "bundle_ref": refs["result"],
        }
        _set_invocation_source(payload)
    return payload


def _set_invocation_source(payload: dict[str, object]) -> None:
    source = payload["source"]
    assert isinstance(source, dict)
    payload["invocation_source"] = {
        (key if key == "candidate_sequence" else f"source_{key}"): value
        for key, value in source.items()
    }
