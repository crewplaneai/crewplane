import asyncio
import json
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.runtime.execution import execute_workflow
from tests.helpers.foreach import (
    compile_foreach_file,
    copy_foreach_fixture,
    freeze_foreach,
    workflow_document,
    write_workflow_document,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    review_output,
)

CONSTRAINT = "Executor findings: JSON array with at most 4 items."


@pytest.mark.parametrize(
    "mode,reviewer", [("parallel", False), ("sequential", False), ("sequential", True)]
)
@pytest.mark.parametrize("items", [[], [{"chunk": "a"}]])
def test_compiled_constraint_reaches_provider_rounds_and_persisted_plan(
    tmp_path: Path, mode: str, reviewer: bool, items: list[dict[str, str]]
) -> None:
    path = copy_foreach_fixture(tmp_path)
    payload, body = workflow_document(path)
    producer = payload["nodes"][1]
    producer["mode"] = mode
    if mode == "sequential":
        producer["depth"] = 2
    if reviewer:
        producer["providers"].append({"provider": "alpha", "role": "reviewer"})
    write_workflow_document(path, payload, body)
    preview = compile_foreach_file(path)
    output = OutputManager("foreach", base_dir=tmp_path)
    freeze_foreach(preview, output)
    restored = PreflightExecutionPlan.model_validate_json(
        (output.stages_dir / "preflight/execution-plan.json").read_bytes()
    )
    render = next(
        render for render in restored.render_plans if render.node_id == "planner"
    )
    for stream in render.streams:
        assert CONSTRAINT in stream.fragments[-1].text
    findings = "<!-- findings -->" + json.dumps(items) + "<!-- /findings -->"
    outputs = [findings]
    expected_roles = ["executor"]
    if reviewer:
        outputs.extend(
            [
                review_output(
                    minor="- Select clearer excerpts", verdict="CHANGES_REQUESTED"
                ),
                "Refined selection.\n" + findings,
                review_output(verdict="NO_FINDINGS"),
            ]
        )
        expected_roles.extend(["reviewer", "executor", "reviewer"])
    elif mode == "sequential":
        outputs.append(findings)
        expected_roles.append("executor")
    outputs.extend(["<!-- findings -->analyzed<!-- /findings -->"] * len(items))
    outputs.append("summary")
    invoker = MockAgentInvoker(outputs=outputs)
    asyncio.run(execute_workflow(restored, output, invoker, preview.secret_context))
    producer_calls = [call for call in invoker.calls if call["node_id"] == "planner"]
    assert [call["role"] for call in producer_calls] == expected_roles
    for call in producer_calls:
        assert CONSTRAINT in call["prompt"]
    for call in invoker.calls[len(producer_calls) :]:
        assert CONSTRAINT not in call["prompt"]
    assert (
        json.loads((output.results_dir / "planner-findings.md").read_bytes()) == items
    )
    index = json.loads((output.results_dir / "chunks-result.md").read_bytes())
    assert [entry["item"] for entry in index] == items
    assert len(invoker.calls) == len(expected_roles) + len(items) + 1
