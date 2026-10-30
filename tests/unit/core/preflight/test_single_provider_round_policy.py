from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from crewplane.core.preflight.compiled_records import ExecutionPolicy
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_policy import (
    maximum_review_round,
    remediation_depth_or_default,
)
from tests.helpers.resume import make_plan


@pytest.mark.parametrize(("depth", "rounds"), [(None, 1), (1, 1), (3, 3)])
def test_single_provider_round_resolution_preserves_policy_wire_contract(
    depth: int | None, rounds: int
) -> None:
    policy = ExecutionPolicy(depth=depth)
    expected = {
        "depth": depth,
        "audit_rounds": None,
        "review_starts_with": "executor",
        "continue_on_failure": False,
        "failure_threshold": None,
        "token_budget": None,
        "consensus_on_exhaustion": None,
        "retry_policy": {},
        "concurrency_policy": {
            "max_concurrent_nodes": None,
            "max_parallel_invocations": None,
        },
    }

    assert policy.single_provider_total_rounds == rounds
    assert policy.model_dump() == expected
    assert json.loads(policy.model_dump_json()) == expected
    assert ExecutionPolicy.model_validate_json(policy.model_dump_json()) == policy
    for schema in (
        ExecutionPolicy.model_json_schema(),
        ExecutionPolicy.model_json_schema(mode="serialization"),
    ):
        assert set(schema["properties"]) == set(expected)
        assert schema["properties"]["depth"] == {
            "anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}],
            "default": None,
            "title": "Depth",
        }
    assert maximum_review_round(remediation_depth_or_default(depth)) == rounds + 1


def test_omitted_single_provider_depth_stays_unresolved() -> None:
    policy = ExecutionPolicy()

    assert policy.single_provider_total_rounds == 1
    assert policy.depth is None
    assert policy.model_dump(exclude_unset=True) == {}
    assert policy.model_dump_json(exclude_unset=True) == "{}"


@pytest.mark.parametrize("field", ["depth", "audit_rounds"])
@pytest.mark.parametrize("value", [True, False, 0, -1])
def test_persisted_round_policy_still_rejects_boolean_and_nonpositive_values(
    field: str, value: bool | int
) -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["execution_policy"][field] = value

    with pytest.raises(ValidationError, match=f"{field} must be greater than 0"):
        PreflightExecutionPlan.model_validate_json(json.dumps(payload))
