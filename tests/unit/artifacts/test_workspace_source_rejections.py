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
    workspace_state_payloads,
)
from crewplane.artifacts.workspace.state.validation import (
    workspace_invocation_source_matches,
)
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.resume import make_plan, replace_plan_fields
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    provider_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import workspace_selection_record


@pytest.mark.parametrize(
    "corruption",
    [
        "descriptors-disagree",
        "unknown-kind",
        "missing-policy",
        "missing-workspace-source",
        "wrong-policy-kind",
        "later-executor",
        "later-reviewer-audit",
        "invalid-reviewer-audit",
    ],
)
def test_project_source_validation_rejects_wrong_context(
    tmp_path: Path, corruption: str
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    fields = plan.model_dump(mode="json")
    fields["nodes"][0]["workspace_policy"] = workspace_selection_record().model_dump(
        mode="json"
    )
    plan = PreflightExecutionPlan.model_validate(fields)
    node = plan.nodes[0]
    source = plan.workspace_source
    assert source is not None
    payload = provider_workspace_state_payload(
        run, plan, source_commit=source.run_base_commit, source_tree=source.source_tree
    )
    assert workspace_invocation_source_matches(run, plan, node, payload)
    if corruption == "descriptors-disagree":
        payload["invocation_source"]["source_commit"] = "f" * 40
    elif corruption == "unknown-kind":
        payload["source"]["kind"] = "unknown"
        payload["invocation_source"]["source_kind"] = "unknown"
    elif corruption == "missing-policy":
        fields["nodes"][0]["workspace_policy"] = None
    elif corruption == "missing-workspace-source":
        fields["workspace_source"] = None
    elif corruption == "wrong-policy-kind":
        fields["nodes"][1]["workspace_policy"] = workspace_selection_record(
            source_kind="node", source_node_id="a"
        ).model_dump(mode="json")
        payload["node_id"] = "b"
    elif corruption == "later-executor":
        payload["round_num"] = 2
    else:
        payload["role"] = "reviewer"
        payload["round_num"] = 0
        payload["workspace"]["lineage_producer"] = False
        payload["audit_round_num"] = 2 if corruption == "later-reviewer-audit" else True

    plan = PreflightExecutionPlan.model_validate(fields)
    node = plan.nodes[1 if corruption == "wrong-policy-kind" else 0]
    assert not workspace_invocation_source_matches(run, plan, node, payload)


@pytest.mark.parametrize(
    "corruption",
    [
        "node-id",
        "round",
        "audit",
        "role",
        "first-executor",
        "missing-prior",
        "prior-round",
        "prior-audit",
        "prior-role",
        "prior-no-lineage",
        "prior-ref",
        "bundle-size",
        "bundle-path",
        "bundle-digest-type",
    ],
)
def test_candidate_sources_require_usable_prior_lineage(
    tmp_path: Path, corruption: str
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    prior = _lineage_payload()
    current = _candidate_payload(prior)
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    prior_file = stage / "workspace-state-prior.json"
    prior_file.write_text(json.dumps(prior))
    assert workspace_invocation_source_matches(run, plan, plan.nodes[0], current)
    if corruption == "node-id":
        current["source"]["node_id"] = "b"
    elif corruption == "round":
        current["round_num"] = True
    elif corruption == "audit":
        current["audit_round_num"] = True
    elif corruption == "role":
        current["role"] = "unknown"
    elif corruption == "first-executor":
        current["round_num"] = 1
    elif corruption == "missing-prior":
        prior_file.unlink()
    elif corruption == "prior-round":
        prior["round_num"] = True
    elif corruption == "prior-audit":
        prior["audit_round_num"] = True
    elif corruption == "prior-role":
        prior["role"] = "reviewer"
    elif corruption == "prior-no-lineage":
        prior["workspace"]["lineage_producer"] = False
    elif corruption == "prior-ref":
        prior["refs"]["result"] = None
    elif corruption == "bundle-size":
        current["source"]["bundle_size_bytes"] = 2
    elif corruption == "bundle-path":
        current["source"]["bundle_path"] = "other.bundle"
    else:
        current["source"]["bundle_sha256"] = None
    current["invocation_source"] = _invocation_source(current["source"])
    if corruption != "missing-prior":
        prior_file.write_text(json.dumps(prior))

    assert not workspace_invocation_source_matches(run, plan, plan.nodes[0], current)


@pytest.mark.parametrize(
    "corruption",
    [
        "wrong-source-node",
        "wrong-policy",
        "later-round",
        "no-lineage",
        "unsafe-status",
    ],
)
def test_node_sources_require_the_selected_upstream_result(
    tmp_path: Path, corruption: str
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    prior = _lineage_payload()
    current = _candidate_payload(prior)
    current["node_id"] = "b"
    current["round_num"] = 1
    current["source"]["kind"] = "node"
    current["invocation_source"] = _invocation_source(current["source"])
    fields = plan.model_dump(mode="json")
    replace_plan_fields(
        fields,
        {
            "nodes/0/workspace_policy": workspace_selection_record().model_dump(
                mode="json"
            ),
            "nodes/1/workspace_policy": workspace_selection_record(
                source_kind="node", source_node_id="a"
            ).model_dump(mode="json"),
        },
    )
    plan = PreflightExecutionPlan.model_validate(fields)
    node = plan.nodes[1]
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    prior_path = stage / "workspace-state-prior.json"
    prior_path.write_text(json.dumps(prior))
    assert workspace_invocation_source_matches(run, plan, node, current)
    if corruption == "wrong-source-node":
        current["source"]["node_id"] = "other"
    elif corruption == "wrong-policy":
        replace_plan_fields(
            fields,
            {
                "nodes/0/workspace_policy": None,
                "nodes/1/workspace_policy": workspace_selection_record().model_dump(
                    mode="json"
                ),
            },
        )
        plan = PreflightExecutionPlan.model_validate(fields)
        node = plan.nodes[1]
    elif corruption == "later-round":
        current["round_num"] = 2
    elif corruption == "no-lineage":
        prior_path.unlink()
    else:
        (stage / "review-state").mkdir()
        (stage / "review-state" / "review-loop-status.json").symlink_to(prior_path)
    current["invocation_source"] = _invocation_source(current["source"])

    assert not workspace_invocation_source_matches(run, plan, node, current)


@pytest.mark.parametrize("content", [b"{", b"null", b"[]", b"\xff"])
def test_invalid_workspace_state_file_invalidates_the_entire_payload_set(
    tmp_path: Path, content: bytes
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    (stage / "workspace-state-valid.json").write_text(json.dumps(_lineage_payload()))
    assert len(workspace_state_payloads(run, plan.nodes[0])) == 1
    (stage / "workspace-state-invalid.json").write_bytes(content)

    assert workspace_state_payloads(run, plan.nodes[0]) == ()


def _lineage_payload() -> dict[str, object]:
    return {
        "node_id": "a",
        "task_id": "alpha",
        "role": "executor",
        "status": "succeeded",
        "round_num": 1,
        "audit_round_num": None,
        "workspace": {"lineage_producer": True},
        "result": {"result_commit": "a" * 40, "result_tree": "b" * 40},
        "bundle": {
            "path": "a/workspace-bundles/prior.bundle",
            "sha256": "c" * 64,
            "size_bytes": 1,
        },
        "refs": {"result": "refs/crewplane/prior/result"},
    }


def _candidate_payload(prior: dict[str, object]) -> dict[str, object]:
    current = deepcopy(prior)
    current["round_num"] = 2
    current["source"] = {
        "kind": "candidate",
        "node_id": "a",
        "commit": prior["result"]["result_commit"],
        "tree": prior["result"]["result_tree"],
        "candidate_sequence": 1,
        "bundle_path": prior["bundle"]["path"],
        "bundle_sha256": prior["bundle"]["sha256"],
        "bundle_size_bytes": prior["bundle"]["size_bytes"],
        "bundle_ref": prior["refs"]["result"],
    }
    current["invocation_source"] = _invocation_source(current["source"])
    return current


def _invocation_source(descriptor: dict[str, object]) -> dict[str, object]:
    return {
        (key if key == "candidate_sequence" else f"source_{key}"): value
        for key, value in descriptor.items()
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "f" * 40),
        ("tree", "f" * 40),
        ("candidate_sequence", 2),
        ("bundle_path", "other.bundle"),
        ("bundle_sha256", "f" * 64),
        ("bundle_sha256", None),
        ("bundle_size_bytes", 2),
    ],
)
def test_source_validation_rejects_each_broken_candidate_link(
    tmp_path, field, value
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    prior = _lineage_payload()
    current = _candidate_payload(prior)
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    (stage / "workspace-state-prior.json").write_text(json.dumps(prior))
    assert workspace_invocation_source_matches(run, plan, plan.nodes[0], current)
    current["source"][field] = value
    current["invocation_source"] = _invocation_source(current["source"])
    assert not workspace_invocation_source_matches(run, plan, plan.nodes[0], current)


@pytest.mark.parametrize(
    ("ref", "source_ref", "matches"),
    [
        (None, None, True),
        (17, None, True),
        (17, 17, False),
    ],
)
def test_source_validation_preserves_non_string_ref_normalization(
    tmp_path, ref, source_ref, matches
) -> None:
    run = source_record(tmp_path)
    plan = make_plan()
    prior = _lineage_payload()
    prior["refs"] = {"result": ref}
    current = _candidate_payload(prior)
    current["source"]["bundle_ref"] = source_ref
    current["invocation_source"] = _invocation_source(current["source"])
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    (stage / "workspace-state-prior.json").write_text(json.dumps(prior))
    assert (
        workspace_invocation_source_matches(run, plan, plan.nodes[0], current)
        is matches
    )


@pytest.mark.parametrize(
    ("source_field", "section", "result_field", "matches"),
    [
        ("commit", "result", "result_commit", True),
        ("tree", "result", "result_tree", True),
        ("bundle_path", "bundle", "path", True),
        ("bundle_size_bytes", "bundle", "size_bytes", True),
        ("bundle_sha256", "bundle", "sha256", False),
    ],
)
@pytest.mark.parametrize("omit", [False, True])
def test_lineage_comparisons_preserve_missing_and_null_semantics(
    tmp_path, source_field, section, result_field, matches, omit
) -> None:
    prior = _lineage_payload()
    current = _candidate_payload(prior)
    descriptor = current["source"]
    descriptor[source_field] = None
    prior[section][result_field] = None
    if omit:
        descriptor.pop(source_field)
    assert _lineage_comparison_results(tmp_path, prior, current) == (matches, matches)


@pytest.mark.parametrize("sequence", [True, 1.0])
def test_lineage_comparisons_preserve_numeric_equality(tmp_path, sequence) -> None:
    prior = _lineage_payload()
    current = _candidate_payload(prior)
    descriptor = current["source"]
    descriptor["candidate_sequence"] = sequence
    descriptor["bundle_size_bytes"] = 1.0
    assert _lineage_comparison_results(tmp_path, prior, current) == (True, True)


def _lineage_comparison_results(
    tmp_path: Path, prior: dict[str, object], current: dict[str, object]
) -> tuple[bool, bool]:
    run = source_record(tmp_path)
    plan = make_plan()
    current["invocation_source"] = _invocation_source(current["source"])
    stage = run.run_dir / "a"
    stage.mkdir(parents=True)
    (stage / "workspace-state-prior.json").write_text(json.dumps(prior))
    expected = (ExpectedWorkspaceInvocation("alpha", ProviderRole.EXECUTOR, 2, None),)
    return (
        workspace_invocation_source_matches(run, plan, plan.nodes[0], current),
        workspace_state_payloads_match_expected_set((prior, current), expected),
    )
