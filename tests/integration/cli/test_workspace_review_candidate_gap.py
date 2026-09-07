from __future__ import annotations

import asyncio
import io
import json
import shutil
from pathlib import Path

import pytest
from rich.console import Console

from crewplane.core.workflow.keywords import ProviderRole
from crewplane.core.workflow.models import (
    PromptSegment,
    ProviderSpec,
    WorkflowNode,
    WorkflowPlan,
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


@pytest.mark.parametrize("resume_after_failure", [False, True])
def test_discarded_review_round_preserves_skip_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    resume_after_failure: bool,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = _review_workflow()
    _write_review_fixtures(fixtures)
    monkeypatch.chdir(project)

    if resume_after_failure:
        with pytest.raises(RuntimeError, match="mock invoker failed"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
        failed_run = run_dirs(project)[0]
        _assert_review_gap(failed_run)
        shutil.rmtree(fixtures / "implement")

    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed candidate.\n")
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
        _assert_review_gap(successful_runs[-1])
        shutil.rmtree(fixtures / "implement")

    duplicate_stream = io.StringIO()
    asyncio.run(
        run_workspace_workflow(workflow, config, Console(file=duplicate_stream))
    )

    assert run_dirs(project) == successful_runs
    assert "Identical context detected" in duplicate_stream.getvalue()


def _assert_review_gap(run_dir: Path) -> None:
    states = workspace_states(run_dir / "implement")
    executors = {
        state["round_num"]: state for state in states if state["role"] == "executor"
    }
    assert set(executors) == {1, 2, 3}
    assert executors[2]["result"]["lineage_discarded"] is True
    assert executors[2]["workspace"]["lineage_producer"] is False
    assert executors[3]["source"]["commit"] == executors[1]["result"]["result_commit"]
    assert sorted(
        state["round_num"] for state in states if state["role"] == "reviewer"
    ) == [
        1,
        3,
    ]


def _review_workflow() -> WorkflowPlan:
    return WorkflowPlan(
        name="WorkspaceReviewGap",
        worktrees={"implementation": {"kind": "worktree"}},
        nodes=[
            WorkflowNode(
                id="implement",
                mode="sequential",
                depth=2,
                worktree="implementation",
                providers=[
                    ProviderSpec(provider="alpha", role=ProviderRole.EXECUTOR),
                    ProviderSpec(provider="alpha", role=ProviderRole.REVIEWER),
                ],
                prompt_segments=[
                    PromptSegment(
                        role="shared",
                        content="Implement and review {{file:src/app.txt}}",
                    )
                ],
            ),
            WorkflowNode(
                id="consume",
                mode="sequential",
                needs=["implement"],
                worktree="implementation",
                providers=[ProviderSpec(provider="alpha")],
                prompt_segments=[
                    PromptSegment(role="shared", content="Read {{file:src/app.txt}}")
                ],
            ),
        ],
    )


def _write_review_fixtures(fixtures: Path) -> None:
    for round_num in (1, 2):
        write_fixture(
            fixtures,
            "implement",
            f"executor-round-{round_num}.md",
            "# Candidate\n\nInitial implementation updates `src/app.txt`.\n",
            sidecar={
                "workspace_mutations": [
                    {"path": "src/app.txt", "content": "first candidate\n"}
                ]
            },
        )
    write_fixture(
        fixtures,
        "implement",
        "executor-round-3.md",
        "# Final candidate\n\nResolved review issues in `src/app.txt`.\n",
        sidecar={
            "required_prompt_contains": ["first candidate"],
            "workspace_mutations": [
                {"path": "src/app.txt", "content": "final candidate\n"}
            ],
        },
    )
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-1.md",
        review_output("CHANGES_REQUESTED", "- Fix the remaining bug."),
    )
    write_fixture(
        fixtures,
        "implement",
        "reviewer-round-3.md",
        review_output("NO_FINDINGS", "None"),
    )
