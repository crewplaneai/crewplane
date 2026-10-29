import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from crewplane.artifacts import OutputManager
from crewplane.artifacts.atomic import atomic_write_json
from crewplane.artifacts.expansions import reconstruct_expansions
from crewplane.artifacts.naming import (
    node_state_relative_path,
    review_checkpoint_relative_path,
)
from crewplane.artifacts.resume import checkpoint_hydration
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.artifacts.run_history import RunHistoryRecord
from crewplane.cli.app import app
from crewplane.cli.run.resume import artifact_valid_history_plan
from crewplane.core.execution_state import RunManifest
from crewplane.core.preflight.models import PreflightExecutionPlan
from crewplane.core.review_checkpoint import (
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
    ReviewLoopCheckpoint,
)
from crewplane.core.review_checkpoint_state import CheckpointProjectObservation
from tests.helpers.foreach import (
    copy_foreach_fixture,
    workflow_document,
    write_workflow_document,
)
from tests.helpers.mock_resume import write_executor_fixture, write_mock_config
from tests.helpers.platforms import requires_resume_support

ORIGINAL_PUBLISH = OutputManager.write_review_checkpoint


def history_record(run_dir: Path) -> RunHistoryRecord:
    path = run_dir / "manifests/run.json"
    manifest = RunManifest.model_validate_json(path.read_bytes())
    return RunHistoryRecord(
        manifest,
        path,
        run_dir,
        run_dir.parent.parent / "execution-results" / run_dir.name,
    )


@pytest.fixture
def paused_generated_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    config, workflow = review_project(root, tmp_path / "fixtures")
    monkeypatch.chdir(root)
    checkpoints = []

    def interrupt(store: OutputManager, record: ReviewLoopCheckpoint) -> Path:
        path = ORIGINAL_PUBLISH(store, record)
        if (
            record.node_id == "chunks.i1.part.analyze"
            and isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == "finalize"
        ):
            checkpoints.append(record)
            raise RuntimeError("pause before finalization")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        result = CliRunner().invoke(
            app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
        )
        assert result.exit_code == 1, result.output
    assert len(checkpoints) == 1
    run_dir = next((root / ".crewplane/execution-stages").iterdir())
    plan = PreflightExecutionPlan.model_validate_json(
        (run_dir / "preflight/execution-plan.json").read_bytes()
    )
    return plan, history_record(run_dir), checkpoints[0]


def review_project(
    root: Path, fixtures: Path, remediation: bool = False
) -> tuple[Path, Path]:
    workflow = copy_foreach_fixture(root)
    child = root / "analyze.md"
    payload, body = workflow_document(child)
    payload["nodes"][-1].update(
        mode="sequential",
        providers=[
            {"provider": "alpha", "role": role} for role in ("executor", "reviewer")
        ],
    )
    if remediation:
        payload["nodes"][-1]["depth"] = 2
    write_workflow_document(child, payload, body)
    config = root / ".crewplane/config.yml"
    write_mock_config(config, fixtures)
    settings = yaml.safe_load(config.read_bytes())
    settings["settings"]["max_concurrent_nodes"] = 1
    config.write_text(yaml.safe_dump(settings), encoding="utf-8", newline="\n")
    write_executor_fixture(
        fixtures,
        "planner",
        '<!-- findings -->[{"chunk":"a"},{"chunk":"b"}]<!-- /findings -->',
    )
    write_executor_fixture(fixtures, "summarize", "Summary")
    for ordinal in range(2):
        directory = fixtures / f"chunks.i{ordinal}.part.analyze"
        directory.mkdir(parents=True)
        (directory / "alpha_executor_0_round1.md").write_bytes(
            b"Implementation\n<!-- findings -->done<!-- /findings -->"
        )
        approved = b"## Major Issues\nNone\n## Minor Issues\nNone\n## Nitpicks\nNone\n---\nVERDICT: NO_FINDINGS\n"
        (directory / "reviewer-round-1.md").write_bytes(approved)
        if remediation:
            (directory / "reviewer-round-1.md").write_bytes(
                b"## Major Issues\n- Fix missing behavior.\n## Minor Issues\nNone\n## Nitpicks\nNone\n---\nVERDICT: CHANGES_REQUESTED\n"
            )
            (directory / "alpha_executor_0_round2.md").write_bytes(
                b"Fixed behavior\n<!-- findings -->fixed<!-- /findings -->"
            )
            (directory / "reviewer-round-2.md").write_bytes(approved)
    return config, workflow


@requires_resume_support
@pytest.mark.parametrize("shift_child_lines", [False, True])
@pytest.mark.parametrize("literal", [False, True])
@pytest.mark.parametrize(
    "phase,expected_roles",
    [
        ("executors", ["executor", "reviewer"]),
        ("reviewers", ["reviewer"]),
        ("finalize", []),
    ],
)
def test_generated_review_checkpoint_resumes_only_unfinished_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_roles: list[str],
    shift_child_lines: bool,
    literal: bool,
) -> None:
    root = tmp_path / "project"
    config, workflow = review_project(root, tmp_path / "fixtures", phase == "executors")
    if literal:
        payload, body = workflow_document(workflow)
        payload["nodes"][2]["items"] = [{"chunk": "a"}, {"chunk": "b"}]
        del payload["nodes"][2]["max_items"]
        write_workflow_document(workflow, payload, body)
    if shift_child_lines:
        # Workflow edits must leave the checkpoint's observed source tree intact.
        definitions = root / ".crewplane/workflows"
        definitions.mkdir()
        for path in root.glob("*.md"):
            path.replace(definitions / path.name)
        workflow = definitions / workflow.name
    monkeypatch.chdir(root)
    clock = Mock()
    clock.now.side_effect = [
        datetime(2026, 10, 28, 12) + timedelta(seconds=i) for i in range(3)
    ]
    monkeypatch.setattr("crewplane.artifacts.directory_manager.datetime", clock)
    runner = CliRunner()
    args = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]

    def interrupt(store: OutputManager, record: ReviewLoopCheckpoint) -> Path:
        path = ORIGINAL_PUBLISH(store, record)
        if (
            record.node_id == "chunks.i1.part.analyze"
            and isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == phase
        ):
            assert record.expansion is not None and record.expansion.ordinal == 1
            raise RuntimeError("generated checkpoint interruption")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        result = runner.invoke(app, args)
        assert result.exit_code == 1, result.output
    if shift_child_lines:
        child = workflow.parent / "analyze.md"
        payload, body = workflow_document(child)
        write_workflow_document(child, payload, "\n\n" + body)
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    runs = sorted((root / ".crewplane/execution-stages").iterdir())
    manifest = json.loads((runs[-1] / "manifests/run.json").read_bytes())
    original_manifest = json.loads((runs[0] / "manifests/run.json").read_bytes())
    assert manifest["workflow_signature"] == original_manifest["workflow_signature"]
    assert manifest["resumed_review_checkpoints"][0]["source_phase"] == phase
    assert (
        manifest["resumed_review_checkpoints"][0]["node_id"] == "chunks.i1.part.analyze"
    )
    assert "chunks.i0.part.analyze" in manifest["resumed_nodes"]
    assert "chunks" not in manifest["resumed_nodes"]
    events = [
        json.loads(line)
        for line in (runs[-1] / "logs/events.ndjson").read_bytes().splitlines()
    ]
    assert [
        event["role"]
        for event in events
        if event["event_type"] == "invocation_started"
        and event["node_id"] == "chunks.i1.part.analyze"
    ] == expected_roles
    assert not any(
        event["event_type"] == "invocation_started"
        and event["node_id"] == "chunks.i0.part.analyze"
        for event in events
    )


@requires_resume_support
def test_generated_checkpoint_hydration_failure_never_invokes_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    config, workflow = review_project(root, tmp_path / "fixtures")
    monkeypatch.chdir(root)
    runner = CliRunner()
    args = ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]

    def interrupt(store: OutputManager, record: ReviewLoopCheckpoint) -> Path:
        path = ORIGINAL_PUBLISH(store, record)
        if (
            isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == "reviewers"
        ):
            raise RuntimeError("checkpoint interruption")
        return path

    with monkeypatch.context() as interruption:
        interruption.setattr(OutputManager, "write_review_checkpoint", interrupt)
        assert runner.invoke(app, args).exit_code == 1

    def fail_copy(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("selected evidence cannot be copied")

    monkeypatch.setattr(checkpoint_hydration, "copy_checkpoint_file", fail_copy)
    failed = runner.invoke(app, args)
    assert failed.exit_code == 1, failed.output
    assert isinstance(failed.exception, OSError)
    assert "selected evidence cannot be copied" in str(failed.exception)
    runs = sorted((root / ".crewplane/execution-stages").iterdir())
    assert len(runs) == 2
    manifest = json.loads((runs[-1] / "manifests/run.json").read_bytes())
    assert manifest["status"] == "failed"
    log = runs[-1] / "logs/events.ndjson"
    if log.exists():
        assert all(
            json.loads(line)["event_type"] != "invocation_started"
            for line in log.read_bytes().splitlines()
        )


@pytest.mark.parametrize("failure", ["review", "findings"])
def test_generated_terminal_failures_publish_expansion_scoped_closed_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "project"
    fixtures = tmp_path / "fixtures"
    config, workflow = review_project(root, fixtures)
    child = root / "analyze.md"
    payload, body = workflow_document(child)
    payload["nodes"][-1]["depth"] = 1
    write_workflow_document(child, payload, body)
    settings = yaml.safe_load(config.read_bytes())
    settings["settings"]["sequential_consensus_on_exhaustion"] = "fatal"
    config.write_text(yaml.safe_dump(settings), encoding="utf-8", newline="\n")
    node_id = "chunks.i1.part.analyze"
    if failure == "review":
        rejection = (
            b"## Major Issues\n- Required behavior is missing.\n## Minor Issues\nNone\n"
            b"## Nitpicks\nNone\n---\nVERDICT: CHANGES_REQUESTED\n"
        )
        (fixtures / node_id / "reviewer-round-1.md").write_bytes(rejection)
        (fixtures / node_id / "reviewer-round-2.md").write_bytes(rejection)
        (fixtures / node_id / "alpha_executor_0_round2.md").write_bytes(
            b"Revised behavior\n<!-- findings -->revised<!-- /findings -->"
        )
    else:
        write_executor_fixture(
            fixtures, node_id, "Missing the required findings block."
        )
    monkeypatch.chdir(root)
    result = CliRunner().invoke(
        app, ["run", "--tasks", str(workflow), "--config", str(config), "--no-live"]
    )
    assert result.exit_code == 1, result.output
    run_dir = next((root / ".crewplane/execution-stages").iterdir())
    plan = PreflightExecutionPlan.model_validate_json(
        (run_dir / "preflight/execution-plan.json").read_bytes()
    )
    effective = reconstruct_expansions(plan, run_dir).plan
    closed = ClosedReviewCheckpoint.model_validate_json(
        (run_dir / review_checkpoint_relative_path(node_id)).read_bytes()
    )
    assert closed.expansion == effective.expansions["chunks"].provenance(1)
    assert not (run_dir / node_state_relative_path(node_id)).exists()
    frontier = validate_resume_frontier(history_record(run_dir), plan)
    assert "chunks.i0.part.analyze" in frontier.resumed_node_ids
    assert not frontier.checkpoint_node_ids
    assert (
        f"chunks:{closed.expansion.digest}:1:{node_id}"
        in frontier.closed_checkpoint_ids
    )


@pytest.mark.parametrize(
    "damage", ["digest", "ordinal", "missing", "review", "unreliable", "prerequisite"]
)
def test_generated_checkpoint_requires_provenance_evidence_and_prerequisites(
    paused_generated_review, damage: str
) -> None:
    plan, source, checkpoint = paused_generated_review
    observation = checkpoint.project_observation
    assert validate_resume_frontier(source, plan, observation).checkpoint_node_ids == (
        checkpoint.node_id,
    )
    payload = checkpoint.model_dump(mode="json")
    if damage == "digest":
        payload["expansion"]["digest"] = "f" * 64
    elif damage == "ordinal":
        payload["expansion"]["ordinal"] = 0
    elif damage == "missing":
        (source.run_dir / checkpoint.files[0].relative_path).unlink()
    elif damage == "review":
        review = checkpoint.progress.reviews()[0]
        (source.run_dir / review.output_path).write_bytes(b"Changed review evidence")
    elif damage == "unreliable":
        observation = CheckpointProjectObservation(
            fingerprint=observation.fingerprint, reliable=False
        )
    else:
        (source.run_dir / node_state_relative_path("chunks.i1.item")).unlink()
    atomic_write_json(
        source.run_dir / review_checkpoint_relative_path(checkpoint.node_id), payload
    )
    frontier = validate_resume_frontier(source, plan, observation)
    assert not frontier.checkpoint_node_ids
    assert "chunks.i0.part.analyze" in frontier.resumed_node_ids
    assert "chunks" not in frontier.resumed_node_ids


@requires_resume_support
@pytest.mark.parametrize(
    "variant",
    ["matching", "different_expansion", "different_ordinal", "invalid_identity"],
)
def test_generated_closed_fences_match_expansion_and_node_before_older_reuse(
    paused_generated_review,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    plan, older, checkpoint = paused_generated_review
    root = Path(plan.project_root)
    if variant == "different_expansion":
        write_executor_fixture(
            tmp_path / "fixtures",
            "planner",
            '<!-- findings -->[{"chunk":"a"},{"chunk":"changed"}]<!-- /findings -->',
        )
    target = (
        "chunks.i0.part.analyze"
        if variant == "different_ordinal"
        else checkpoint.node_id
    )
    closed_records = []

    def terminal_failure(store: OutputManager, record: ReviewLoopCheckpoint) -> Path:
        if (
            record.node_id == target
            and isinstance(record, OpenReviewCheckpoint)
            and record.next_phase == "finalize"
        ):
            closed = ClosedReviewCheckpoint.model_validate(
                {
                    **{
                        key: value
                        for key, value in record.model_dump().items()
                        if key in ClosedReviewCheckpoint.model_fields and key != "kind"
                    },
                    "terminal_reason": "review_failure",
                }
            )
            ORIGINAL_PUBLISH(store, closed)
            closed_records.append(closed)
            raise RuntimeError("terminal generated review failure")
        return ORIGINAL_PUBLISH(store, record)

    with monkeypatch.context() as failure:
        failure.setattr(OutputManager, "write_review_checkpoint", terminal_failure)
        result = CliRunner().invoke(
            app,
            [
                "run",
                "--tasks",
                str(root / "parent.md"),
                "--config",
                str(root / ".crewplane/config.yml"),
                "--no-live",
                "--force",
            ],
        )
        assert result.exit_code == 1, result.output
    assert len(closed_records) == 1
    newer = history_record(
        next(
            path
            for path in (root / ".crewplane/execution-stages").iterdir()
            if path != older.run_dir
        )
    )
    effective = reconstruct_expansions(plan, newer.run_dir).plan
    for node in effective.nodes:
        (newer.run_dir / node_state_relative_path(node.id)).unlink(missing_ok=True)
    if variant == "invalid_identity":
        payload = closed_records[0].model_dump(mode="json")
        payload["expansion"]["ordinal"] = 0
        atomic_write_json(
            newer.run_dir / review_checkpoint_relative_path(target), payload
        )
    selected = artifact_valid_history_plan(
        older.manifest.workflow_identity, (newer, older), plan, root / ".crewplane"
    )
    assert selected.decision.resume_source == older
    assert "chunks.i0.part.analyze" in selected.resumed_node_ids
    assert selected.checkpoint_node_ids == (
        () if variant == "matching" else (checkpoint.node_id,)
    )
