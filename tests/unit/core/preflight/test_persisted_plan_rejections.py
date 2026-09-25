from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from crewplane.core.preflight import PreflightExecutionPlan
from tests.helpers.resume import make_plan, replace_plan_fields


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"nodes/1/dependencies": ["a", "a"]}, "duplicate dependencies"),
        ({"nodes/0/render_plan_id": "missing"}, "invalid render_plan_id"),
        ({"nodes/0/render_plan_id": "b"}, "invalid render_plan_id"),
        ({"nodes/0/id": " "}, "nonblank id"),
        ({"render_plans/0/render_plan_id": " "}, "nonblank render_plan_id"),
        ({"render_plans/1/render_plan_id": "a"}, "duplicate render plan"),
        ({"execution_order": ["a", "a", "b"]}, "duplicate nodes"),
        (
            {"nodes/0/mode": "parallel", "nodes/0/execution_policy/depth": 2},
            "cannot define depth",
        ),
        (
            {"nodes/0/mode": "parallel", "nodes/0/execution_policy/audit_rounds": 2},
            "cannot define depth",
        ),
        (
            {
                "nodes/0/mode": "parallel",
                "nodes/0/execution_policy/review_starts_with": "reviewer",
            },
            "must start with executors",
        ),
        (
            {
                "nodes/0/mode": "parallel",
                "nodes/0/execution_policy/consensus_on_exhaustion": "continue",
            },
            "cannot define consensus",
        ),
        (
            {"nodes/0/mode": "parallel", "nodes/0/provider_records/0/role": "reviewer"},
            "cannot contain reviewers",
        ),
        (
            {
                "nodes/0/mode": "parallel",
                "nodes/0/execution_policy/failure_threshold": 1,
            },
            "less than its provider count",
        ),
        (
            {"nodes/0/execution_policy/failure_threshold": 0},
            "cannot define failure threshold",
        ),
        ({"nodes/0/provider_records/0/role": "reviewer"}, "must use an executor"),
        (
            {"nodes/0/execution_policy/consensus_on_exhaustion": "continue"},
            "outside a sequential review loop",
        ),
        ({"runtime_config_snapshot/execution": []}, "must contain an execution object"),
        (
            {"runtime_config_snapshot/execution": {"max_concurrent_nodes": 2}},
            "concurrency policy conflicts",
        ),
        (
            {"nodes/0/provider_records/0/provider": " "},
            "provider provider cannot be blank",
        ),
        (
            {"nodes/0/provider_records/0/agent_config_key": " "},
            "agent_config_key cannot be blank",
        ),
        (
            {"nodes/0/provider_records/0/invoker_alias": " "},
            "invoker_alias cannot be blank",
        ),
        (
            {"nodes/0/provider_records/0/agent_config_signature": " "},
            "agent_config_signature cannot be blank",
        ),
        (
            {"nodes/0/provider_records/0/invoker_config_signature": " "},
            "invoker_config_signature cannot be blank",
        ),
        ({"nodes/0/provider_records/0/task_id": " "}, "task_id cannot be blank"),
        ({"dependency_graph/0/source_node": " "}, "node ids cannot be blank"),
        ({"dependency_graph/0/target_node": "unknown"}, "unknown target node"),
        (
            {
                "dependency_graph/0/source_node": "b",
                "dependency_graph/0/target_node": "a",
            },
            "must point from an upstream node",
        ),
        ({"dependency_graph/0/artifact_name": "missing"}, "unknown artifact"),
        ({"dependency_graph/0/artifact_name": "findings"}, "does not publish findings"),
        (
            {"dependency_graph/0/artifact_key": "findings"},
            "artifact_key is inconsistent",
        ),
        (
            {"dependency_graph/0/target_locator": "a.findings"},
            "target_locator is inconsistent",
        ),
        ({"nodes/0/artifact_contract/stage_path": None}, "missing its stage locator"),
        (
            {"nodes/0/artifact_contract/stage_path": "preflight/owned"},
            "reserved stage root",
        ),
        (
            {"nodes/0/artifact_contract/result_path": "other.md"},
            "conflicting output locators",
        ),
        (
            {"nodes/0/artifact_contract/findings_path": "findings.md"},
            "inconsistent findings locator",
        ),
        (
            {"fingerprint_metadata/payload_version": "obsolete"},
            "fingerprint metadata payload_version",
        ),
        (
            {"value_fingerprints": [{"fingerprint_payload_version": "obsolete"}]},
            "payload version must be",
        ),
    ],
)
def test_persisted_plan_rejects_inconsistent_execution_contracts(
    changes: dict[str, object], message: str
) -> None:
    payload = make_plan().model_dump(mode="json")
    replace_plan_fields(payload, changes)

    with pytest.raises(ValidationError, match=message):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"nodes/0/provider_records/0/role": "reviewer"},
            "must contain executor and reviewer",
        ),
        (
            {"nodes/0/provider_records/1/role": "executor"},
            "must contain executor and reviewer",
        ),
        ({"nodes/0/provider_records/1/task_id": "alpha"}, "duplicate provider task_id"),
        (
            {"nodes/0/execution_policy/consensus_on_exhaustion": None},
            "missing consensus policy",
        ),
        (
            {
                "nodes/0/provider_records/0/role": "reviewer",
                "nodes/0/provider_records/1/role": "executor",
            },
            "must group executors before reviewers",
        ),
    ],
)
def test_persisted_review_plan_rejects_broken_provider_roles_and_identity(
    changes: dict[str, object], message: str
) -> None:
    payload = make_plan(review_loop=True).model_dump(mode="json")
    replace_plan_fields(payload, changes)

    with pytest.raises(ValidationError, match=message):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


def test_persisted_plan_rejects_duplicate_dependency_edges() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["dependency_graph"] *= 2

    with pytest.raises(ValidationError, match="duplicate edge"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "roles, message",
    [
        (
            [],
            "Persisted provider preflight node 'a' must define at least one provider record.",
        ),
        (["executor"], None),
        (["reviewer"], "Persisted single-provider node 'a' must use an executor."),
        (
            ["executor", "executor"],
            "Persisted review-loop node 'a' must contain executor and reviewer providers in that order.",
        ),
        (
            ["reviewer", "reviewer"],
            "Persisted review-loop node 'a' must contain executor and reviewer providers in that order.",
        ),
        (["executor", "executor", "reviewer", "reviewer"], None),
        (
            ["reviewer", "executor"],
            "Persisted review-loop node 'a' must group executors before reviewers.",
        ),
        (
            ["executor", "reviewer", "executor"],
            "Persisted review-loop node 'a' must group executors before reviewers.",
        ),
    ],
)
def test_persisted_sequential_role_boundary(
    roles: list[str], message: str | None
) -> None:
    payload = make_plan(review_loop=len(roles) > 1).model_dump(mode="json")
    prototype = payload["nodes"][0]["provider_records"][0]
    payload["nodes"][0]["provider_records"] = [
        {**prototype, "role": role, "task_id": f"task-{index}"}
        for index, role in enumerate(roles)
    ]

    if message is not None:
        with pytest.raises(ValidationError) as caught:
            PreflightExecutionPlan.model_validate_json(json.dumps(payload))
        assert [error["msg"] for error in caught.value.errors()] == [
            f"Value error, {message}"
        ]
    else:
        restored = PreflightExecutionPlan.model_validate_json(json.dumps(payload))
        assert [
            provider.role for provider in restored.nodes[0].provider_records
        ] == roles
