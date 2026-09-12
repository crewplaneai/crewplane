from __future__ import annotations

import pytest

from crewplane.core.preflight.models import ExecutionPolicy, TokenBudgetPolicy
from crewplane.core.preflight.secrets import SecretContext
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.runtime.execution import prompt_budgeting
from crewplane.runtime.execution.activity.telemetry import ExecutionTelemetry
from crewplane.runtime.execution.fragment_assembler import ResolvedPrompt
from crewplane.runtime.execution.review_loop.prompts import (
    resolve_previous_candidate_context,
)
from crewplane.runtime.execution.review_loop.types import ExecutorRoundArtifact
from crewplane.runtime.execution.runtime_context import CompiledRuntimeContext
from crewplane.runtime.execution.workspace_files import ResolvedWorkspaceFile
from tests.helpers.resume import make_plan, make_workspace_file_locator, sha256_hex


def test_prompt_budget_rejects_resolved_workspace_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    locator = make_workspace_file_locator().model_copy(
        update={
            "locator_id": "workspace-file-b",
            "node_id": "b",
            "target": "executor_prompt",
            "byte_size": 20,
            "canonical_blob_sha256": sha256_hex("x" * 20),
        }
    )
    node = plan.nodes[1].model_copy(
        update={
            "execution_policy": ExecutionPolicy(
                token_budget=TokenBudgetPolicy(fail_threshold_chars=10)
            )
        }
    )
    plan = plan.model_copy(
        update={
            "nodes": [plan.nodes[0], node],
            "workspace_file_locators": [locator],
        }
    )
    resolved_file = ResolvedWorkspaceFile(
        locator=locator,
        text="x" * 20,
        byte_size=20,
        sha256=sha256_hex("x" * 20),
    )

    def inspect_runtime_locators_stub(*args: object) -> tuple[object, ...]:
        del args
        return ()

    def assemble_prompt_details_stub(
        *args: object,
        **kwargs: object,
    ) -> ResolvedPrompt:
        del args, kwargs
        return ResolvedPrompt(resolved_file.text, (resolved_file,))

    monkeypatch.setattr(
        prompt_budgeting,
        "inspect_runtime_locators",
        inspect_runtime_locators_stub,
    )
    monkeypatch.setattr(
        prompt_budgeting,
        "assemble_prompt_details",
        assemble_prompt_details_stub,
    )

    with pytest.raises(
        prompt_budgeting.PromptBudgetExceededError,
        match="workspace file 'docs/input.md' resolves to 20 chars",
    ):
        prompt_budgeting.resolve_prompt_with_output_budget_details(
            CompiledRuntimeContext(plan=plan, secret_context=SecretContext()),
            node,
            object(),
            ProviderRole.EXECUTOR,
            None,
        )


@pytest.mark.parametrize(
    ("fail_offset", "warn_offset", "outcome"),
    [
        (None, None, "disabled"),
        (None, None, "absent"),
        (0, None, "pass"),
        (-1, None, "fail"),
        (None, 0, "pass"),
        (None, -1, "warn"),
        (0, -1, "warn"),
        (-1, -1, "fail"),
    ],
)
def test_previous_candidate_budget_boundaries(
    tmp_path, fail_offset, warn_offset, outcome
) -> None:
    node = make_plan().nodes[0]
    output_file = tmp_path / "candidate.md"
    artifact = ExecutorRoundArtifact(
        provider=node.provider_records[0],
        task_id="alpha",
        content="candidate é",
        output_file=output_file,
        audit_round_num=None,
        round_num=1,
    )
    context = f"=== alpha executor output ===\nArtifact: {output_file}\n\ncandidate é"
    char_count = len(context)
    fail_threshold = None if fail_offset is None else char_count + fail_offset
    warn_threshold = None if warn_offset is None else char_count + warn_offset
    policy = (
        None
        if outcome == "absent"
        else TokenBudgetPolicy(
            fail_threshold_chars=fail_threshold, warn_threshold_chars=warn_threshold
        )
    )
    node = node.model_copy(
        update={"execution_policy": ExecutionPolicy(token_budget=policy)}
    )
    events = []
    telemetry = ExecutionTelemetry(
        workflow_name="workflow", run_id="run", event_sink=events.append
    )

    if outcome == "fail":
        with pytest.raises(prompt_budgeting.PromptBudgetExceededError) as caught:
            resolve_previous_candidate_context(node, [artifact], telemetry)
        assert str(caught.value) == (
            "Prompt budget exceeded for node 'a': previous canonical candidate resolves to "
            f"{char_count} chars, exceeding fail threshold {fail_threshold}. "
            "Shorten the prior candidate or raise the threshold intentionally."
        )
    else:
        assert (
            resolve_previous_candidate_context(node, [artifact], telemetry) == context
        )
    if outcome != "warn":
        assert events == []
        return
    assert len(events) == 1
    event = events[0]
    assert event.context.node_id == "a"
    assert event.payload.level == "warning"
    assert event.payload.operation == "prompt_budget_warning"
    assert event.payload.message == (
        "Prompt budget warning for node 'a': previous canonical candidate resolves to "
        f"{char_count} chars, exceeding warn threshold {warn_threshold}. "
        "Shorten the prior candidate or raise the threshold intentionally."
    )
    assert event.payload.attributes == {
        "upstream_node_id": "a",
        "upstream_artifact_name": "previous_canonical_candidate",
        "char_count": char_count,
        "warn_threshold_chars": warn_threshold,
    }
