from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
from crewplane.architecture.contracts import InvocationContext
from crewplane.core.config import AgentConfig
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
)
from crewplane.runtime.agent.failures import (
    InvocationFailureError,
    InvocationFailureSummary,
)
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_workflow_fixtures import (
    review_output,
    run_dirs,
    workspace_states,
    write_fixture,
)
from tests.helpers.workspace_workflow_runner import (
    run_workspace_workflow,
    workspace_config,
    workspace_project,
)

isolated_git = isolated_git_support.isolated_git


@pytest.mark.parametrize("continue_on_failure", [False, True])
@pytest.mark.parametrize("resume_after_failure", [False, True])
def test_discarded_remediation_failure_preserves_skip_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    continue_on_failure: bool,
    resume_after_failure: bool,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = _review_workflow(continue_on_failure)
    write_fixture(fixtures, "implement", "executor-round-1.md", "Implemented.\n")
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-1.md",
        review_output("CHANGES_REQUESTED", "- Address the remaining issue."),
    )
    original_invoke = MockAgentInvoker.invoke

    async def invoke_with_context_exhaustion(
        self: MockAgentInvoker,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None:
        assert invocation_context is not None
        if invocation_context.role == "executor" and invocation_context.round_num == 2:
            raise InvocationFailureError(
                "simulated provider failure",
                InvocationFailureSummary(
                    kind="provider_session_context_exhausted",
                    phase="provider_session",
                    source="stdout_json",
                    message="Provider session context exhausted.",
                    advice="Start a new session.",
                    condensed=False,
                ),
                None,
            )
        await original_invoke(
            self, config, model, prompt, output_file, cwd, log_file, invocation_context
        )

    monkeypatch.setattr(MockAgentInvoker, "invoke", invoke_with_context_exhaustion)
    monkeypatch.chdir(project)
    if resume_after_failure:
        with pytest.raises(RuntimeError, match="mock invoker failed"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
        _assert_recovered_candidate(run_dirs(project)[0])
        shutil.rmtree(fixtures / "implement")

    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed.\n")
    stream = io.StringIO()
    asyncio.run(run_workspace_workflow(workflow, config, Console(file=stream)))
    successful_runs = run_dirs(project)
    manifest = json.loads(
        (successful_runs[-1] / "manifests" / "run.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "succeeded"
    if resume_after_failure:
        assert "Resuming workflow" in stream.getvalue()
        assert manifest["resumed_nodes"] == ["implement"]
    else:
        _assert_recovered_candidate(successful_runs[-1])
        shutil.rmtree(fixtures / "implement")

    duplicate_stream = io.StringIO()
    asyncio.run(
        run_workspace_workflow(workflow, config, Console(file=duplicate_stream))
    )

    assert run_dirs(project) == successful_runs
    assert "Identical context detected" in duplicate_stream.getvalue()


def _assert_recovered_candidate(run_dir: Path) -> None:
    executors = {
        state["round_num"]: state
        for state in workspace_states(run_dir / "implement")
        if state["role"] == "executor"
    }
    assert executors[1]["status"] == "succeeded"
    assert executors[1]["workspace"]["lineage_producer"] is True
    assert executors[2]["status"] == "failed"
    assert executors[2]["workspace"]["lineage_producer"] is False
    assert (
        executors[2]["result"]["lineage_discard_reason"]
        == "remediation_context_exhausted"
    )
    assert executors[2]["source"]["commit"] == executors[1]["result"]["result_commit"]


def _review_workflow(continue_on_failure: bool) -> WorkflowPlan:
    return WorkflowPlan(
        name="RemediationContextExhaustion",
        worktrees={"code": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                depth=1,
                continue_on_failure=continue_on_failure,
                providers=[
                    ProviderSpec(provider="alpha"),
                    ProviderSpec(provider="alpha", role="reviewer"),
                ],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Implement and review {{file:src/app.txt}}.",
                    )
                ],
            ),
            WorkflowNode(
                id="consume",
                mode="sequential",
                needs=["implement"],
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[PromptSegment(role="shared", content="Consume.")],
            ),
        ],
    )
