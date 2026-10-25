from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.core.preflight.models import ExecutionPolicy
from tests.helpers.platforms import requires_workspace_support
from tests.helpers.resume import (
    attach_workspace_descriptor,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import (
    attach_git_workspace_source,
    snapshot_workspace_state_payload,
    source_record,
)
from tests.helpers.workspace_records import workspace_selection_record

pytestmark = requires_workspace_support


@pytest.mark.parametrize(("depth", "final_round"), [(None, 1), (1, 1), (3, 3)])
@pytest.mark.parametrize("evidence", ["correct", "wrong", "missing"])
def test_resume_requires_single_provider_final_round_evidence(
    tmp_path: Path, depth: int | None, final_round: int, evidence: str
) -> None:
    source = source_record(tmp_path)
    plan = make_plan()
    node = plan.nodes[0].model_copy(
        update={
            "execution_policy": ExecutionPolicy(depth=depth),
            "workspace_policy": workspace_selection_record(
                enabled=True,
                kind="snapshot",
                clean_start="strict",
                materialization="snapshot_checkout",
            ),
        }
    )
    plan = plan.model_copy(update={"nodes": [node, plan.nodes[1]]})
    plan, _repo = attach_git_workspace_source(tmp_path, plan)
    descriptor = write_result(source.results_dir, "a-result.md", "a output")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    stage_dir = source.run_dir / "a"
    stage_dir.mkdir(exist_ok=True)
    payload = snapshot_workspace_state_payload(source, plan, "alpha")
    payload["round_num"] = (
        (2 if final_round == 1 else 1) if evidence == "wrong" else final_round
    )
    state_path = stage_dir / "workspace-state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    attach_workspace_descriptor(source.run_dir, plan, "a")
    if evidence == "missing":
        state_path.unlink()

    frontier = validate_resume_frontier(source, plan)

    assert frontier.resumed_node_ids == (("a",) if evidence == "correct" else ())
