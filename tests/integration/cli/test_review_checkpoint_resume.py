from __future__ import annotations

import asyncio
import io
import json
from functools import partial
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from crewplane.artifacts import OutputManager
from crewplane.artifacts.resume import checkpoint_hydration
from crewplane.cli.app import app
from crewplane.cli.workflow_runner import execute_workflow_run
from crewplane.core.config import load_config
from crewplane.core.preflight import load_workflow_source_for_preflight
from crewplane.core.review_checkpoint import OpenReviewCheckpoint, ReviewLoopCheckpoint
from tests.helpers.review_checkpoint_cli import write_checkpoint_project
from tests.integration.cli.repeat_force_run_support import filesystem_snapshot


def interrupt_review(store, record):
    path = ORIGINAL_PUBLISH(store, record)
    if isinstance(record, OpenReviewCheckpoint) and record.next_phase == "reviewers":
        raise RuntimeError("checkpoint interruption")
    return path


ORIGINAL_PUBLISH = OutputManager.write_review_checkpoint


@pytest.mark.parametrize("predecessor", [False, True])
def test_dry_run_reports_checkpoint_collection_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, predecessor: bool
) -> None:
    root = tmp_path / "project"
    config, workflow = write_checkpoint_project(
        root, tmp_path / "fixtures", predecessor
    )
    monkeypatch.chdir(root)
    runner = CliRunner()
    args = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    with monkeypatch.context() as interrupt:
        interrupt.setattr(OutputManager, "write_review_checkpoint", interrupt_review)
        failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    before = filesystem_snapshot(root)
    preview = runner.invoke(app, [*args, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert f"would_resume {int(predecessor)} node(s)" in preview.output
    assert "Review checkpoints: 1 checkpoint(s)" in preview.output
    assert "nodes: review.iterate" in preview.output
    if predecessor:
        assert "nodes: requirements" in " ".join(preview.output.split())
    assert filesystem_snapshot(root) == before
    forced = runner.invoke(app, [*args, "--dry-run", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "would_execute_full_run" in forced.output
    assert filesystem_snapshot(root) == before
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    runs = sorted((root / ".crewplane/execution-stages").iterdir())
    manifest = json.loads((runs[-1] / "manifests/run.json").read_bytes())
    assert manifest["resumed_nodes"] == (["requirements"] if predecessor else [])
    assert manifest["resumed_review_checkpoints"][0]["node_id"] == "review.iterate"
    events = [
        json.loads(line)
        for line in (runs[-1] / "logs/events.ndjson").read_text().splitlines()
    ]
    starts = [event for event in events if event["event_type"] == "invocation_started"]
    assert [(event["node_id"], event["role"]) for event in starts] == [
        ("review.iterate", "reviewer"),
        ("after", "executor"),
    ]
    assert any(
        event.get("operation") == "review_checkpoint_resumed" for event in events
    )


@pytest.mark.parametrize("state_name", [".crewplane", "custom-state"])
@pytest.mark.parametrize("phase", ["reviewers", "finalize"])
@pytest.mark.parametrize("project_changed", [False, True])
def test_checkpoint_resume_observes_project_outside_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_name: str,
    phase: str,
    project_changed: bool,
) -> None:
    root = tmp_path / "project"
    config, workflow = write_checkpoint_project(root, tmp_path / "fixtures")
    state_dir = root / state_name
    project_file = root / "source.txt"
    project_file.write_text("original project contents")
    run_workflow = partial(
        execute_workflow_run,
        config=load_config(config),
        source=load_workflow_source_for_preflight(workflow, project_root=root),
        force=False,
        no_live=True,
        console=Console(file=io.StringIO()),
        project_root=root,
        state_dir=state_dir,
    )

    def interrupt(store: OutputManager, record: ReviewLoopCheckpoint) -> Path:
        path = ORIGINAL_PUBLISH(store, record)
        if isinstance(record, OpenReviewCheckpoint) and record.next_phase == phase:
            raise RuntimeError("checkpoint interruption")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        with pytest.raises(RuntimeError, match="checkpoint interruption"):
            asyncio.run(run_workflow())
    if project_changed:
        project_file.write_text("changed project contents")
    asyncio.run(run_workflow())

    runs = sorted((state_dir / "execution-stages").iterdir())
    assert len(runs) == 2
    manifest = json.loads((runs[-1] / "manifests/run.json").read_bytes())
    assert manifest["status"] == "succeeded"
    summaries = manifest["resumed_review_checkpoints"]
    assert [item["node_id"] for item in summaries] == (
        [] if project_changed else ["review.iterate"]
    )
    if summaries:
        assert summaries[0]["source_phase"] == phase
        assert manifest["resume_source_run_key_name"] == runs[0].name
    events = [
        json.loads(line)
        for line in (runs[-1] / "logs/events.ndjson").read_text().splitlines()
    ]
    roles = [
        event["role"]
        for event in events
        if event["event_type"] == "invocation_started"
        and event["node_id"] == "review.iterate"
    ]
    if project_changed:
        assert roles == ["executor", "reviewer"]
    else:
        assert roles == (["reviewer"] if phase == "reviewers" else [])


@pytest.mark.parametrize("failure", ["copy", "manifest", "setup"])
def test_checkpoint_setup_failure_terminalizes_fresh_run_without_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "project"
    config, workflow = write_checkpoint_project(root, tmp_path / "fixtures")
    monkeypatch.chdir(root)
    runner = CliRunner()
    args = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    with monkeypatch.context() as interrupt:
        interrupt.setattr(OutputManager, "write_review_checkpoint", interrupt_review)
        assert runner.invoke(app, args).exit_code == 1

    def fail(*args, **kwargs):
        del args, kwargs
        raise ValueError("checkpoint setup test failure")

    if failure == "copy":
        monkeypatch.setattr(checkpoint_hydration, "copy_checkpoint_file", fail)
    elif failure == "manifest":
        monkeypatch.setattr(OutputManager, "record_hydrated_review_checkpoint", fail)
    else:
        read_summaries = OutputManager.read_hydrated_review_checkpoints

        def mismatched_provenance(store):
            return [
                item.model_copy(
                    update={"source_local_round": item.source_local_round + 1}
                )
                for item in read_summaries(store)
            ]

        monkeypatch.setattr(
            OutputManager, "read_hydrated_review_checkpoints", mismatched_provenance
        )
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.output
    runs = sorted((root / ".crewplane/execution-stages").iterdir())
    assert len(runs) == 2
    manifest = json.loads((runs[-1] / "manifests/run.json").read_bytes())
    assert manifest["status"] == "failed"
    log = runs[-1] / "logs/events.ndjson"
    events = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    assert not any(event["event_type"] == "invocation_started" for event in events)
