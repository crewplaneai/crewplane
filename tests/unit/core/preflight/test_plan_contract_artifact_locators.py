from __future__ import annotations

import pytest

from crewplane.core.preflight.models import PreflightExecutionPlan
from tests.helpers.resume import make_plan


@pytest.mark.parametrize("reserved_root", ["logs", "manifests", "preflight"])
def test_persisted_plan_rejects_reserved_stage_roots(reserved_root: str) -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][0]["artifact_contract"]["stage_path"] = reserved_root
    payload["nodes"][0]["artifact_contract"]["log_path"] = f"{reserved_root}/logs"

    with pytest.raises(ValueError, match="reserved stage root"):
        PreflightExecutionPlan.model_validate(payload)


def test_persisted_plan_rejects_hierarchically_overlapping_stage_paths() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][1]["artifact_contract"].update(
        {
            "stage_path": "a/nested",
            "log_path": "a/nested/logs",
        }
    )

    with pytest.raises(ValueError, match="overlapping stages artifact locators"):
        PreflightExecutionPlan.model_validate(payload)


def test_persisted_plan_rejects_hierarchically_overlapping_result_paths() -> None:
    payload = make_plan().model_dump(mode="json")
    payload["nodes"][1]["artifact_contract"].update(
        {
            "output_path": "a-result.md/child",
            "result_path": "a-result.md/child",
        }
    )

    with pytest.raises(ValueError, match="overlapping results artifact locators"):
        PreflightExecutionPlan.model_validate(payload)
