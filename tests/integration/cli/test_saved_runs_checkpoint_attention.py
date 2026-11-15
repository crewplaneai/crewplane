from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.artifacts import OutputManager
from crewplane.artifacts.naming import review_checkpoint_relative_path
from crewplane.artifacts.run_history import RunHistoryRecord, scan_run_history
from crewplane.cli.app import app
from crewplane.cli.runs.listing import saved_run_rows
from crewplane.observability.events import EventType
from crewplane.version import SCHEMA_VERSION
from tests.helpers.mock_resume import write_mock_config
from tests.helpers.platforms import requires_resume_support, symlink_or_skip
from tests.helpers.saved_runs import saved_event, saved_run, write_events
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    review_output,
)


def _resume_finalized_review(
    root: Path, monkeypatch: pytest.MonkeyPatch, decision: str = "consensus_exhausted"
) -> RunHistoryRecord:
    project = root / "project"
    fixtures = root / "fixtures"
    config = project / ".crewplane/config.yml"
    write_mock_config(config, fixtures)
    workflow = project / "workflow.task.md"
    workflow.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "schema_version": SCHEMA_VERSION,
                "name": "Saved review attention",
                "nodes": [
                    {
                        "id": "review",
                        "mode": "sequential",
                        "depth": 3 if decision == "no_progress" else 1,
                        "audit_rounds": 1,
                        "continue_on_failure": True,
                        "providers": [
                            {"provider": "alpha", "role": role}
                            for role in ("executor", "reviewer")
                        ],
                    }
                ],
            }
        )
        + "---\n## review\nReview the candidate.\n",
        encoding="utf-8",
        newline="\n",
    )
    (fixtures / "review").mkdir(parents=True)
    review = (
        review_output(verdict="NO_FINDINGS")
        if decision == "consensus"
        else review_output(major="- Fix the defect", verdict="CHANGES_REQUESTED")
    )
    (fixtures / "review/reviewer.md").write_bytes(review.encode())
    for round_num in range(1, 5):
        candidate = (
            "Unchanged candidate" if decision == "no_progress" else str(round_num)
        )
        (fixtures / f"review/alpha_executor_0_round{round_num}.md").write_bytes(
            candidate.encode()
        )
    monkeypatch.chdir(project)
    runner = CliRunner()
    args = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]

    def fail_finalization(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("interrupted result finalization")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(OutputManager, "finalize_node", fail_finalization)
        failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    record = scan_run_history(project / ".crewplane").records[0]
    assert record.manifest.resumed_review_checkpoints[0].source_phase == "finalize"
    return record


@requires_resume_support
@pytest.mark.parametrize(
    "decision", ["consensus_exhausted", "no_progress", "consensus"]
)
def test_finalized_resume_retains_review_attention(tmp_path, monkeypatch, decision):
    record = _resume_finalized_review(tmp_path, monkeypatch, decision)
    status = json.loads(
        (record.run_dir / "review/review-state/review-loop-status.json").read_bytes()
    )
    assert status["stop_reason"] == decision
    events = [
        json.loads(line)
        for line in (record.run_dir / "logs/events.ndjson").read_bytes().splitlines()
    ]
    assert not any(event["event_type"] == "invocation_started" for event in events)
    row = saved_run_rows(scan_run_history(record.run_dir.parent.parent))[0]
    assert row.attention == (
        "none recorded" if decision == "consensus" else "review unresolved"
    )


@requires_resume_support
def test_completed_reuse_retains_finalized_checkpoint_attention(tmp_path, monkeypatch):
    source = _resume_finalized_review(tmp_path, monkeypatch)
    state = source.run_dir.parent.parent
    reused = saved_run(
        state,
        "reused",
        "workflow--reused",
        workflow_name=source.manifest.workflow_name,
        workflow_identity=source.manifest.workflow_identity,
        workflow_signature=source.manifest.workflow_signature,
        started_at="2100-01-01T00:00:00Z",
        completed_at="2100-01-01T00:00:01Z",
        resumed_nodes=["review"],
        resume_source_run_id=source.manifest.run_id,
        resume_source_run_key_name=source.manifest.run_key_name,
    )
    write_events(reused, [saved_event(reused, EventType.WORKFLOW_FINISHED)])
    assert saved_run_rows(scan_run_history(state))[0].attention == "review unresolved"


@requires_resume_support
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "corrupt",
        "symlink",
        "run_id",
        "run_key_name",
        "workflow_identity",
        "workflow_name",
        "workflow_signature",
        "node_id",
        "resume_origin",
        "next_phase",
        "audit",
        "local_round",
    ],
)
def test_unavailable_finalized_decision_is_incomplete(tmp_path, monkeypatch, damage):
    record = _resume_finalized_review(tmp_path, monkeypatch)
    path = record.run_dir / review_checkpoint_relative_path("review")
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"{")
    elif damage == "symlink":
        moved = path.with_name("moved-checkpoint.json")
        path.rename(moved)
        symlink_or_skip(path, moved)
    else:
        payload = json.loads(path.read_bytes())
        if damage == "resume_origin":
            payload[damage]["source_run_id"] = "another-run"
        else:
            payload[damage] = {
                "workflow_signature": "0" * 64,
                "next_phase": "executors",
                "audit": 2,
                "local_round": 7,
            }.get(damage, "another-value")
        path.write_bytes(json.dumps(payload).encode())
    assert (
        saved_run_rows(scan_run_history(record.run_dir.parent.parent))[0].attention
        == "evidence incomplete"
    )


@requires_resume_support
@pytest.mark.parametrize("failure", [OSError, ValueError, RuntimeError])
def test_finalized_checkpoint_read_failure_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    record = _resume_finalized_review(tmp_path, monkeypatch)
    history = scan_run_history(record.run_dir.parent.parent)

    def fail_checkpoint_read(root: Path, node_id: str) -> None:
        del root, node_id
        raise failure("checkpoint read failed")

    monkeypatch.setattr(
        "crewplane.cli.runs.listing.read_review_checkpoint", fail_checkpoint_read
    )
    if failure is RuntimeError:
        with pytest.raises(RuntimeError, match="checkpoint read failed"):
            saved_run_rows(history)
    else:
        assert saved_run_rows(history)[0].attention == "evidence incomplete"
