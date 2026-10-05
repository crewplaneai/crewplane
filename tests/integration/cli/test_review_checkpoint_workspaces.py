from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from threading import Event

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.artifacts import OutputManager
from crewplane.cli.app import app
from crewplane.core.review_checkpoint import OpenReviewCheckpoint
from tests.helpers import isolated_git as isolated_git_support
from tests.helpers.isolated_git import IsolatedGit
from tests.helpers.workspace_review import (
    review_workflow,
    write_review_fixtures,
)
from tests.helpers.workspace_workflow_fixtures import run_dirs, write_fixture
from tests.helpers.workspace_workflow_runner import (
    run_workspace_workflow,
    workspace_config,
    workspace_project,
)

isolated_git = isolated_git_support.isolated_git


def test_slow_workspace_verification_leaves_event_loop_and_registry_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
) -> None:
    from crewplane.artifacts.workspace import checkpoint_state
    from crewplane.runtime.execution.review_loop import orchestration

    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = review_workflow()
    write_review_fixtures(fixtures)
    write_fixture(fixtures, "consume", "executor-round-1.md", "Consumed candidate.\n")
    monkeypatch.chdir(project)
    started, release = Event(), Event()
    contexts = []
    commit = orchestration.commit_checkpoint
    validate = checkpoint_state.checkpoint_invocation_is_valid

    async def capture_context(context, *args):
        contexts.append(context)
        return await commit(context, *args)

    def slow_validation(*args):
        started.set()
        assert release.wait(10), "workspace verification blocked the event loop"
        return validate(*args)

    monkeypatch.setattr(orchestration, "commit_checkpoint", capture_context)
    monkeypatch.setattr(
        checkpoint_state, "checkpoint_invocation_is_valid", slow_validation
    )

    async def run():
        task = asyncio.create_task(
            run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
        )
        try:
            assert await asyncio.to_thread(started.wait, 10)
            assert not task.done()
            assert contexts[0].runtime_context.runtime_publications.snapshot()[0]
            release.set()
            await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "audit_rounds,boundary,reviewer_failure",
    [
        (1, (1, 1, "reviewers"), False),
        (1, (1, 2, "executors"), False),
        (1, (1, 3, "executors"), False),
        (1, (1, 3, "reviewers"), False),
        (1, (1, 3, "finalize"), False),
        (2, (2, 1, "reviewers"), False),
        (2, (2, 1, "finalize"), False),
        (1, (1, 2, "executors"), True),
    ],
)
def test_managed_checkpoint_survives_physical_cleanup_and_preserves_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    audit_rounds: int,
    boundary: tuple[int, int, str],
    reviewer_failure: bool,
) -> None:
    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    config = workspace_config(tmp_path / "cache", fixtures)
    workflow = review_workflow()
    workflow.nodes[0].audit_rounds = audit_rounds
    if reviewer_failure:
        workflow.nodes[0].continue_on_failure = True
        from crewplane.adapters.invokers.mock_invoker.invoker import MockAgentInvoker
        from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
            provider_failure,
        )

        invoke = MockAgentInvoker.invoke

        async def fail_reviewer(invoker, *args, **kwargs):
            await invoke(invoker, *args, **kwargs)
            context = kwargs["invocation_context"]
            if (
                context.node_id == "implement"
                and context.role == "reviewer"
                and context.round_num == 1
            ):
                raise provider_failure("settled reviewer failure")

        monkeypatch.setattr(MockAgentInvoker, "invoke", fail_reviewer)
    write_review_fixtures(fixtures)
    from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
        review_output,
    )

    write_fixture(
        fixtures,
        "implement",
        "review-audit-round-2/reviewer-round-1.md",
        review_output(verdict="NO_FINDINGS"),
    )
    write_fixture(
        fixtures,
        "consume",
        "executor-round-1.md",
        "Consumed candidate.\n",
        sidecar={"required_prompt_contains": ["final candidate"]},
    )
    monkeypatch.chdir(project)
    publish = OutputManager.write_review_checkpoint

    def interrupt(store, record):
        path = publish(store, record)
        if (
            isinstance(record, OpenReviewCheckpoint)
            and (record.audit, record.local_round, record.next_phase) == boundary
        ):
            raise RuntimeError("checkpoint interruption")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        with pytest.raises(RuntimeError, match="checkpoint interruption"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
    source = run_dirs(project)[0]
    marker_path = next((source / "manifests/review-checkpoints").glob("*.json"))
    marker = OpenReviewCheckpoint.model_validate_json(marker_path.read_bytes())
    immutable = {
        item.relative_path: (source / item.relative_path).read_bytes()
        for item in marker.files
    }
    config_path = project / ".crewplane/cleanup-config.yml"
    config_path.write_text(config.model_dump_json())
    result = CliRunner().invoke(
        app,
        ["cleanup", "workspaces", "--config", str(config_path), "--yes"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    config_path.unlink()
    assert immutable == {path: (source / path).read_bytes() for path in immutable}
    assert all(
        "workspace-state-" not in Path(item.snapshot_path).name
        for item in marker.workspaces
    )
    asyncio.run(run_workspace_workflow(workflow, config, Console(file=io.StringIO())))
    fresh = run_dirs(project)[-1]
    manifest = json.loads((fresh / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded"
    assert manifest["resumed_nodes"] == []
    assert manifest["resumed_review_checkpoints"][0]["node_id"] == "implement"
    events = [
        json.loads(line)
        for line in (fresh / "logs/events.ndjson").read_text().splitlines()
    ]
    invocations = [
        event
        for event in events
        if event.get("event_type") == "invocation_started"
        and event.get("node_id") == "implement"
    ]
    if boundary[2] == "finalize":
        assert not invocations
    state = next((fresh / "manifests/nodes").glob("implement--*.json"))
    assert (
        json.loads(state.read_bytes())["resume_origin"]["source_run_id"]
        == marker.run_id
    )
    assert immutable == {path: (source / path).read_bytes() for path in immutable}
    before = run_dirs(project)
    stream = io.StringIO()
    asyncio.run(run_workspace_workflow(workflow, config, Console(file=stream)))
    assert run_dirs(project) == before
    assert "Identical context" in stream.getvalue()


@pytest.mark.parametrize(
    "damage",
    ["bundle", "ref", "tree", "parent", "rendered", "setup", "drain", "source_closure"],
)
def test_checkpoint_workspace_rejects_invalid_semantic_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: IsolatedGit,
    damage: str,
) -> None:
    import sys

    from crewplane.artifacts.atomic import atomic_write_json
    from crewplane.artifacts.resume.checkpoint_validation import (
        require_checkpoint_dependencies,
    )
    from crewplane.core.config import Config
    from crewplane.core.file_hashing import file_size_and_sha256
    from crewplane.core.preflight.models import PreflightExecutionPlan

    project = workspace_project(tmp_path, isolated_git)
    fixtures = tmp_path / "fixtures"
    payload = workspace_config(tmp_path / "cache", fixtures).model_dump()
    payload["settings"]["workspace"]["setup_profiles"] = {
        "prepare": {"run": [[sys.executable, "-c", "print('ready')"]]}
    }
    config = Config.model_validate(payload)
    workflow = review_workflow()
    workflow.worktrees["implementation"].setup_profile = "prepare"
    write_review_fixtures(fixtures)
    monkeypatch.chdir(project)
    publish = OutputManager.write_review_checkpoint

    def interrupt(store, record):
        path = publish(store, record)
        if (
            isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == "executors"
        ):
            raise RuntimeError("checkpoint interruption")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        with pytest.raises(RuntimeError, match="checkpoint interruption"):
            asyncio.run(
                run_workspace_workflow(workflow, config, Console(file=io.StringIO()))
            )
    source = run_dirs(project)[0]
    marker = OpenReviewCheckpoint.model_validate_json(
        next((source / "manifests/review-checkpoints").glob("*.json")).read_bytes()
    )
    plan = PreflightExecutionPlan.model_validate_json(
        (source / "preflight/execution-plan.json").read_bytes()
    )
    require_checkpoint_dependencies(source, plan, plan.nodes[0], marker)
    if damage in {"bundle", "setup"}:
        purpose = "workspace_bundle" if damage == "bundle" else "workspace_setup"
        descriptor = next(item for item in marker.files if item.purpose == purpose)
        (source / descriptor.relative_path).unlink()
    elif damage == "source_closure":
        marker = marker.model_copy(update={"workspaces": marker.workspaces[1:]})
    else:
        workspace = next(item for item in marker.workspaces if item.role == "executor")
        path = source / workspace.snapshot_path
        state = json.loads(path.read_bytes())
        if damage == "ref":
            state["refs"]["result"] += "-missing"
        elif damage == "tree":
            state["result"]["result_tree"] = "0" * 40
        elif damage == "parent":
            state["source"]["commit"] = "0" * 40
        elif damage == "rendered":
            state["rendered_workspace_files"] = []
        else:
            state["process_drain"]["status"] = "unresolved"
        atomic_write_json(path, state)
        marker = marker.model_copy(
            update={
                "files": [
                    item.model_copy(update={"signature": file_size_and_sha256(path)})
                    if item.relative_path == workspace.snapshot_path
                    else item
                    for item in marker.files
                ]
            }
        )
    with pytest.raises(ValueError):
        require_checkpoint_dependencies(source, plan, plan.nodes[0], marker)
