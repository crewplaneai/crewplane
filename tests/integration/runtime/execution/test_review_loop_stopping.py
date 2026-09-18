import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.artifacts import OutputManager
from crewplane.artifacts.results.review_loop_status import (
    resolve_review_loop_status,
    review_loop_status_path,
)
from crewplane.core.config import AgentConfig, Config
from crewplane.core.prompt_segments import PromptSegmentRole
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import PromptSegment, ProviderSpec, WorkflowNode
from crewplane.runtime.execution.common import ExecutionTelemetry
from crewplane.runtime.execution.errors import NodeExecutionError
from crewplane.version import SCHEMA_VERSION
from tests.helpers.artifacts import node_artifact_request
from tests.integration.runtime.execution.review_loop_rounds_support import review_output
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    MockAgentInvoker,
    execute_sequential_stage,
)

CANDIDATE = "# Candidate\n\nThe implementation and its acceptance evidence.\n"
UNRESOLVED = review_output(
    "CHANGES_REQUESTED", "- Add the missing acceptance evidence."
)


class ScriptedInvoker(MockAgentInvoker):
    def __init__(
        self, outputs: list[str], actions: dict[int, Callable[[], None]]
    ) -> None:
        super().__init__(outputs)
        self.actions = actions

    async def invoke(
        self,
        config,
        model,
        prompt,
        output_file,
        cwd,
        log_file=None,
        invocation_context=None,
    ) -> None:
        action = self.actions.get(len(self.calls) + 1)
        if action is not None:
            action()
        await super().invoke(
            config, model, prompt, output_file, cwd, log_file, invocation_context
        )


def review_node(depth: int = 10, audit_rounds: int = 5) -> WorkflowNode:
    return WorkflowNode(
        id="review.node",
        mode="sequential",
        prompt_segments=[
            PromptSegment(role=PromptSegmentRole.SHARED, content="Review this.")
        ],
        depth=depth,
        audit_rounds=audit_rounds,
        providers=[
            ProviderSpec(provider="exec", role=ProviderRole.EXECUTOR),
            ProviderSpec(provider="review", role=ProviderRole.REVIEWER),
        ],
    )


def review_config() -> Config:
    return Config(
        version=SCHEMA_VERSION,
        agents={
            "exec": AgentConfig(cli_cmd=["mock"], default_model="m1"),
            "review": AgentConfig(cli_cmd=["mock"], default_model="m2"),
        },
    )


def status_for(output: OutputManager, node: WorkflowNode) -> dict:
    node_dir = output.get_node_dir(node_artifact_request(node.id))
    assert node_dir is not None
    assert resolve_review_loop_status(node.id, node_dir) is not None
    return json.loads(review_loop_status_path(node_dir).read_text())


@pytest.mark.parametrize("continue_on_failure", [False, True])
@pytest.mark.parametrize(
    "depth, expected_audits, expected_calls", [(10, 1, 4), (1, 2, 5)]
)
def test_identical_attempts_stop_the_entire_node(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continue_on_failure: bool,
    depth: int,
    expected_audits: int,
    expected_calls: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    node = review_node(depth=depth)
    node.continue_on_failure = continue_on_failure
    outputs = (
        [CANDIDATE, UNRESOLVED, CANDIDATE, CANDIDATE]
        if depth == 10
        else [CANDIDATE, UNRESOLVED, CANDIDATE, UNRESOLVED, CANDIDATE]
    )
    invoker = MockAgentInvoker(outputs)
    output = OutputManager("workflow", base_dir=tmp_path)
    invocation = execute_sequential_stage(review_config(), node, output, invoker)
    if continue_on_failure:
        asyncio.run(invocation)
    else:
        with pytest.raises(NodeExecutionError, match="no_progress"):
            asyncio.run(invocation)

    assert len(invoker.calls) == expected_calls
    assert "final recovery attempt" in invoker.calls[-1]["prompt"]
    assert "missing acceptance evidence" in invoker.calls[-1]["prompt"]
    status = status_for(output, node)
    assert status["executed_audit_rounds"] == expected_audits
    assert status["stop_reason"] == "no_progress"
    assert status["no_progress_round_count"] == 2
    assert status["consecutive_no_progress_round_count"] == 2
    assert status["continued_after_stop"] is continue_on_failure
    assert status["continued_after_consensus_exhaustion"] is False
    assert status["consensus_reached"] is False
    assert status["final_local_round_num"] == 1
    assert status["reviewer_outputs"][0]["round_num"] == 1


def test_recovery_can_change_candidate_then_pass_a_fresh_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    node = review_node()
    invoker = MockAgentInvoker(
        [
            CANDIDATE,
            UNRESOLVED,
            CANDIDATE,
            CANDIDATE + "Evidence added.",
            review_output(),
            review_output(),
        ]
    )
    output = OutputManager("workflow", base_dir=tmp_path)

    asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    assert len(invoker.calls) == 6
    status = status_for(output, node)
    assert status["consensus_reached"] is True
    assert status["stop_reason"] == "consensus"
    assert status["no_progress_round_count"] == 1
    assert status["consecutive_no_progress_round_count"] == 0
    assert status["executed_audit_rounds"] == 2


def test_changed_source_with_identical_handoff_is_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "program.py"
    node = review_node(audit_rounds=1)
    invoker = ScriptedInvoker(
        [CANDIDATE, UNRESOLVED, CANDIDATE, review_output()],
        {
            1: lambda: source.write_text("if True:\n    run()\nstop()\n"),
            3: lambda: source.write_text("if True:\n    run()\n    stop()\n"),
        },
    )
    output = OutputManager("workflow", base_dir=tmp_path)

    asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    assert len(invoker.calls) == 4
    status = status_for(output, node)
    assert status["consensus_reached"] is True
    assert status["no_progress_round_count"] == 0
    assert status["final_local_round_num"] == 2


def test_rewording_handoff_does_not_count_as_a_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "program.py"
    invoker = ScriptedInvoker(
        [
            CANDIDATE,
            UNRESOLVED,
            CANDIDATE + " Still blocked.",
            CANDIDATE + " Acceptance is unavailable.",
        ],
        {1: lambda: source.write_text("run()\n")},
    )
    node = review_node()
    output = OutputManager("workflow", base_dir=tmp_path)

    with pytest.raises(NodeExecutionError, match="no_progress"):
        asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    assert len(invoker.calls) == 4
    assert status_for(output, node)["no_progress_round_count"] == 2


def test_indentation_only_document_change_is_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = "```python\nif True:\n    run()\nstop()\n```"
    revised = "```python\nif True:\n    run()\n    stop()\n```"
    invoker = MockAgentInvoker([original, UNRESOLVED, revised, review_output()])
    node = review_node(audit_rounds=1)
    output = OutputManager("workflow", base_dir=tmp_path)

    asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    assert len(invoker.calls) == 4
    assert status_for(output, node)["no_progress_round_count"] == 0


@pytest.mark.parametrize("cancel_during_review", [False, True])
def test_cancellation_preserves_latest_completed_review_and_live_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_during_review: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    node = review_node()
    output = OutputManager("workflow", base_dir=tmp_path)
    changed = CANDIDATE + "A partial fix."
    later = changed + "A further fix." if cancel_during_review else changed
    status_before_cancellation = []

    def cancel() -> None:
        status_before_cancellation.append(status_for(output, node))
        raise asyncio.CancelledError

    invoker = ScriptedInvoker(
        [CANDIDATE, UNRESOLVED, changed, UNRESOLVED, later], {6: cancel}
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    status = status_for(output, node)
    assert status["stop_reason"] == "cancelled"
    assert status["consensus_reached"] is False
    assert status["attempted_local_round_num"] == (3 if cancel_during_review else 4)
    assert status["no_progress_round_count"] == (0 if cancel_during_review else 1)
    assert (
        status_before_cancellation[0]["no_progress_round_count"]
        == status["no_progress_round_count"]
    )
    assert status["final_local_round_num"] == 2
    assert status["canonical_executor_outputs"][0]["round_num"] == 2
    assert status["reviewer_outputs"][0]["round_num"] == 2


def test_uncertain_project_attribution_runs_reviewers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from crewplane.runtime.execution.activity.telemetry import RuntimeActivityTracker

    monkeypatch.chdir(tmp_path)
    tracker = RuntimeActivityTracker()
    tracker.mark_node_running("another.node")
    node = review_node(audit_rounds=1)
    output = OutputManager("workflow", base_dir=tmp_path)
    invoker = MockAgentInvoker([CANDIDATE, UNRESOLVED, CANDIDATE, review_output()])

    asyncio.run(
        execute_sequential_stage(
            review_config(),
            node,
            output,
            invoker,
            ExecutionTelemetry("workflow", "run-1", activity_tracker=tracker),
        )
    )

    assert len(invoker.calls) == 4
    assert status_for(output, node)["no_progress_round_count"] == 0


def test_cancellation_in_fresh_audit_preserves_previous_review_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    node = review_node(depth=2)
    output = OutputManager("workflow", base_dir=tmp_path)
    changed = CANDIDATE + "A partial fix."

    def cancel() -> None:
        assert status_for(output, node)["no_progress_round_count"] == 1
        raise asyncio.CancelledError

    invoker = ScriptedInvoker(
        [CANDIDATE, UNRESOLVED, changed, UNRESOLVED, changed], {6: cancel}
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execute_sequential_stage(review_config(), node, output, invoker))

    status = status_for(output, node)
    assert status["stop_reason"] == "cancelled"
    assert status["executed_audit_rounds"] == 2
    assert status["attempted_local_round_num"] == 1
    assert status["final_local_round_num"] == 2
    assert status["no_progress_round_count"] == 1
    assert status["canonical_executor_outputs"][0]["audit_round_num"] == 1
    assert status["reviewer_outputs"][0]["audit_round_num"] == 1
