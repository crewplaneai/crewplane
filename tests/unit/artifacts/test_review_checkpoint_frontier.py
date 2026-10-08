from __future__ import annotations

import hashlib
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from crewplane.artifacts.resume import validation
from crewplane.artifacts.resume.checkpoint_store import publish_review_checkpoint
from crewplane.artifacts.resume.validation import validate_resume_frontier
from crewplane.cli.run.resume import artifact_valid_history_plan
from crewplane.core.execution_state import ResumeOrigin, ReviewCheckpointResumeSummary
from crewplane.core.preflight.workspace.models import WorkspaceSelectionRecord
from crewplane.core.review_checkpoint import (
    ClosedReviewCheckpoint,
    OpenReviewCheckpoint,
)
from crewplane.core.review_checkpoint_state import CheckpointProjectObservation
from crewplane.runtime.execution.review_loop.candidate_identity import (
    project_fingerprint,
)
from tests.helpers.platforms import requires_resume_support
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    iso_datetime,
    make_node_state,
    make_plan,
    write_node_state,
    write_result,
)
from tests.helpers.resume_validation import source_record
from tests.helpers.review_checkpoints import checkpoint_payload


def checkpoint_history(tmp_path: Path):
    source = source_record(tmp_path)
    plan = make_plan(review_loop=True).model_copy(
        update={"project_root": str(tmp_path)}
    )
    payload = checkpoint_payload()
    payload["workflow_identity"] = source.manifest.workflow_identity
    payload["files"][0]["signature"] = (9, hashlib.sha256(b"candidate").hexdigest())
    fingerprint = project_fingerprint(tmp_path, (source.run_dir, source.results_dir))
    payload["project_observation"] = {"fingerprint": fingerprint, "reliable": True}
    checkpoint = OpenReviewCheckpoint.model_validate(payload)
    path = source.run_dir / checkpoint.files[0].relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"candidate")
    from crewplane.artifacts.atomic import atomic_write_json
    from crewplane.artifacts.resume.checkpoint_files import describe_checkpoint_file
    from crewplane.core.review_checkpoint_state import CheckpointInvocation

    candidate = checkpoint.progress.candidates()[0]
    identity_path = path.with_suffix(".candidate.json")
    atomic_write_json(identity_path, candidate.identity.model_dump(mode="json"))
    descriptor = describe_checkpoint_file(
        source.run_dir,
        identity_path.relative_to(source.run_dir).as_posix(),
        CheckpointInvocation(
            task_id=candidate.task_id, role=candidate.role, audit=1, local_round=1
        ),
        "candidate_identity",
    )
    checkpoint = checkpoint.model_copy(
        update={"files": [*checkpoint.files, descriptor]}
    )
    publish_review_checkpoint(source.run_dir, checkpoint)
    return (
        source,
        plan,
        checkpoint,
        CheckpointProjectObservation(fingerprint=fingerprint, reliable=True),
    )


def test_root_checkpoint_frontier_does_not_release_dependents(tmp_path: Path) -> None:
    source, plan, _, observation = checkpoint_history(tmp_path)
    descriptor = write_result(source.results_dir, "b-result.md", "premature dependent")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "b", [descriptor])
    )
    frontier = validate_resume_frontier(source, plan, observation)
    assert frontier.resumed_node_ids == ()
    assert frontier.checkpoint_node_ids == ("a",)


@pytest.mark.parametrize(
    "damage",
    [
        "project",
        "unreliable",
        "missing",
        "signature",
        "workflow",
        "topology",
        "unknown_field",
        "bounds",
    ],
)
def test_invalid_historical_checkpoint_is_unavailable(
    tmp_path: Path, damage: str
) -> None:
    source, plan, record, observation = checkpoint_history(tmp_path)
    payload = record.model_dump(mode="json")
    if damage == "project":
        observation = CheckpointProjectObservation(fingerprint="c" * 64, reliable=True)
    elif damage == "unreliable":
        observation = CheckpointProjectObservation(
            fingerprint=observation.fingerprint, reliable=False
        )
    elif damage == "missing":
        (source.run_dir / record.files[0].relative_path).unlink()
    else:
        match damage:
            case "signature":
                payload["workflow_signature"] = "c" * 64
            case "workflow":
                payload["workflow_identity"] = "different"
            case "topology":
                payload["tasks"].reverse()
            case "unknown_field":
                payload["surprise"] = True
            case "bounds":
                payload["audit"] = 100
        from crewplane.artifacts.atomic import atomic_write_json
        from crewplane.artifacts.naming import review_checkpoint_relative_path

        atomic_write_json(
            source.run_dir / review_checkpoint_relative_path("a"), payload
        )
    assert validate_resume_frontier(source, plan, observation).checkpoint_node_ids == ()


def test_successful_state_wins_over_same_node_checkpoint(tmp_path: Path) -> None:
    source, plan, _, observation = checkpoint_history(tmp_path)
    descriptor = write_result(source.results_dir, "a-result.md", "complete")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    frontier = validate_resume_frontier(source, plan, observation)
    assert frontier.resumed_node_ids == ("a",)
    assert frontier.checkpoint_node_ids == ()


@pytest.mark.parametrize("provenance", ["missing", "mismatched", "orphan", "matching"])
def test_historical_checkpoint_requires_matching_hydration_origin(
    tmp_path: Path, provenance: str
) -> None:
    source, plan, record, observation = checkpoint_history(tmp_path)
    origin = ResumeOrigin(
        source_run_id="earlier",
        source_run_key_name="workflow--earlier",
        source_node_id="a",
        hydrated_at=iso_datetime(),
    )
    publish_review_checkpoint(
        source.run_dir,
        record.model_copy(
            update={"resume_origin": None if provenance == "orphan" else origin}
        ),
    )
    if provenance != "missing":
        summary = ReviewCheckpointResumeSummary(
            node_id="a",
            source_audit=1,
            source_local_round=1,
            source_phase="executors",
            resume_origin=(
                origin.model_copy(update={"hydrated_at": iso_datetime(2)})
                if provenance == "mismatched"
                else origin
            ),
        )
        source = replace(
            source,
            manifest=source.manifest.model_copy(
                update={
                    "resumed_review_checkpoints": [summary],
                    "resume_source_run_id": origin.source_run_id,
                    "resume_source_run_key_name": origin.source_run_key_name,
                }
            ),
        )
    frontier = validate_resume_frontier(source, plan, observation)
    assert frontier.checkpoint_node_ids == (("a",) if provenance == "matching" else ())


def test_closed_marker_needs_no_files_and_fences_only_partial_reuse(
    tmp_path: Path,
) -> None:
    source, plan, record, observation = checkpoint_history(tmp_path)
    closed = ClosedReviewCheckpoint.model_validate(
        {
            **{
                key: value
                for key, value in record.model_dump().items()
                if key in ClosedReviewCheckpoint.model_fields and key != "kind"
            },
            "terminal_reason": "no_progress",
        }
    )
    publish_review_checkpoint(source.run_dir, closed)
    (source.run_dir / record.files[0].relative_path).unlink()
    frontier = validate_resume_frontier(source, plan, observation)
    assert frontier.closed_checkpoint_ids == {"a"}
    assert not frontier.checkpoints


def test_fences_leave_successful_node_reuse_available(tmp_path: Path) -> None:
    source, plan, _, observation = checkpoint_history(tmp_path)
    assert not validate_resume_frontier(
        source, plan, observation, frozenset({"a"})
    ).checkpoints
    descriptor = write_result(source.results_dir, "a-result.md", "complete")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    assert validate_resume_frontier(
        source, plan, observation, frozenset({"a"})
    ).resumed_node_ids == ("a",)


@pytest.mark.parametrize("history_status", [None, "running", "succeeded"])
def test_history_without_failed_attempts_does_not_scan_project(
    tmp_path: Path, history_status: str | None
) -> None:
    plan = make_plan(review_loop=True).model_copy(
        update={"project_root": str(tmp_path)}
    )
    records = (
        () if history_status is None else (source_record(tmp_path, history_status),)
    )
    with patch(
        "crewplane.cli.run.resume.project_fingerprint", wraps=project_fingerprint
    ) as scan:
        selected = artifact_valid_history_plan(
            WORKFLOW_IDENTITY, records, plan, tmp_path
        )
    scan.assert_not_called()
    assert selected.decision.kind == "execute_full"


@requires_resume_support
@pytest.mark.parametrize("history_status", ["failed", "cancelled"])
def test_history_without_review_loops_reuses_nodes_without_scanning_project(
    tmp_path: Path, history_status: str
) -> None:
    source = source_record(tmp_path, history_status)
    plan = make_plan().model_copy(update={"project_root": str(tmp_path)})
    descriptor = write_result(source.results_dir, "a-result.md", "complete")
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "a", [descriptor])
    )
    with patch(
        "crewplane.cli.run.resume.project_fingerprint", wraps=project_fingerprint
    ) as scan:
        selected = artifact_valid_history_plan(
            source.manifest.workflow_identity, (source,), plan, tmp_path
        )
    scan.assert_not_called()
    assert selected.decision.kind == "resume"
    assert selected.resumed_node_ids == ("a",)


def test_managed_review_loop_history_does_not_scan_project(tmp_path: Path) -> None:
    source = source_record(tmp_path)
    plan = make_plan(review_loop=True).model_copy(
        update={"project_root": str(tmp_path)}
    )
    policy = WorkspaceSelectionRecord(
        enabled=True,
        logical_worktree_name="review",
        declaration_kind="snapshot",
        materialization="snapshot_checkout",
        writable=True,
    )
    plan.nodes[0] = plan.nodes[0].model_copy(update={"workspace_policy": policy})
    with patch(
        "crewplane.cli.run.resume.project_fingerprint", wraps=project_fingerprint
    ) as scan:
        selected = artifact_valid_history_plan(
            source.manifest.workflow_identity, (source,), plan, tmp_path
        )
    scan.assert_not_called()
    assert selected.decision.kind == "execute_full"


@requires_resume_support
def test_history_selects_checkpoint_only_run_and_success_first(tmp_path: Path) -> None:
    source, plan, _, _ = checkpoint_history(tmp_path)
    selected = artifact_valid_history_plan(
        source.manifest.workflow_identity, (source,), plan, tmp_path / "artifacts"
    )
    assert selected.decision.kind == "resume"
    assert selected.resumed_node_ids == ()
    assert selected.checkpoint_node_ids == ("a",)
    for node in plan.nodes:
        descriptor = write_result(
            source.results_dir, f"{node.id}-result.md", "complete"
        )
        write_node_state(
            source.run_dir, make_node_state(source.manifest, node.id, [descriptor])
        )
    succeeded = source.__class__(
        source.manifest.model_copy(
            update={"status": "succeeded", "failure_message": None}
        ),
        source.manifest_path,
        source.run_dir,
        source.results_dir,
    )
    assert (
        artifact_valid_history_plan(
            source.manifest.workflow_identity, (source, succeeded), plan, tmp_path
        ).decision.kind
        == "skip"
    )


@requires_resume_support
@pytest.mark.parametrize("valid_closed", [True, False])
def test_newer_closed_only_attempt_fences_older_partial_but_keeps_other_nodes(
    tmp_path: Path, valid_closed: bool
) -> None:
    source, plan, marker, _ = checkpoint_history(tmp_path)
    plan = plan.model_copy(
        update={
            "dependency_graph": [],
            "nodes": [
                plan.nodes[0],
                plan.nodes[1].model_copy(update={"dependencies": []}),
            ],
        }
    )
    descriptor = write_result(
        source.results_dir, "b-result.md", "independent complete output"
    )
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "b", [descriptor])
    )
    newer = replace(
        source,
        manifest=source.manifest.model_copy(
            update={"run_id": "newer", "run_key_name": "workflow--newer"}
        ),
        run_dir=source.run_dir.parent / "workflow--newer",
    )
    identity = {
        key: value
        for key, value in marker.model_dump().items()
        if key in ClosedReviewCheckpoint.model_fields and key != "kind"
    }
    closed = ClosedReviewCheckpoint(
        **{
            **identity,
            "run_id": "newer",
            "run_key_name": "workflow--newer",
            "terminal_reason": "no_progress",
            "workflow_signature": marker.workflow_signature
            if valid_closed
            else "f" * 64,
        }
    )
    publish_review_checkpoint(newer.run_dir, closed)
    selected = artifact_valid_history_plan(
        source.manifest.workflow_identity, (newer, source), plan, tmp_path
    )
    assert selected.decision.resume_source == source
    assert selected.resumed_node_ids == ("b",)
    assert selected.checkpoint_node_ids == (() if valid_closed else ("a",))


def test_checkpoint_requires_successful_dependency_closure(tmp_path: Path) -> None:
    source, plan, _, observation = checkpoint_history(tmp_path)
    prerequisite = plan.nodes[1].model_copy(update={"dependencies": []})
    review = plan.nodes[0].model_copy(update={"dependencies": ["b"]})
    edge = plan.dependency_graph[0].model_copy(
        update={"source_node": "b", "target_node": "a"}
    )
    plan = plan.model_copy(
        update={
            "nodes": [prerequisite, review],
            "execution_order": ["b", "a"],
            "dependency_graph": [edge],
        }
    )
    assert not validate_resume_frontier(source, plan, observation).checkpoints
    descriptor = write_result(
        source.results_dir, "b-result.md", "prerequisite completed"
    )
    write_node_state(
        source.run_dir, make_node_state(source.manifest, "b", [descriptor])
    )
    frontier = validate_resume_frontier(source, plan, observation)
    assert frontier.resumed_node_ids == ("b",)
    assert frontier.checkpoint_node_ids == ("a",)


@pytest.mark.parametrize(
    ("failed_check", "error_type"),
    [
        (None, None),
        ("read_review_checkpoint", PermissionError),
        ("checkpoint_matches_source", ValueError),
        ("validate_checkpoint_node", ValueError),
        ("_require_checkpoint_provenance", ValueError),
        ("require_checkpoint_dependencies", OSError),
        ("require_checkpoint_project", RuntimeError),
        ("require_checkpoint_dependencies", TypeError),
    ],
)
def test_checkpoint_selection_preserves_validation_order_and_error_handling(
    tmp_path: Path, failed_check: str | None, error_type: type[Exception] | None
) -> None:
    source, plan, checkpoint, observation = checkpoint_history(tmp_path)
    checks = [
        "read_review_checkpoint",
        "checkpoint_matches_source",
        "validate_checkpoint_node",
        "_require_checkpoint_provenance",
        "require_checkpoint_dependencies",
        "require_checkpoint_project",
    ]
    error = None if error_type is None else error_type("checkpoint check failed")
    contents = {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    }
    calls = Mock()
    with ExitStack() as stack:
        for name in checks:
            spy = stack.enter_context(
                patch.object(
                    validation,
                    name,
                    wraps=getattr(validation, name),
                    side_effect=error if name == failed_check else None,
                )
            )
            calls.attach_mock(spy, name)
        if error_type is TypeError:
            with pytest.raises(TypeError, match="checkpoint check failed") as caught:
                validate_resume_frontier(source, plan, observation)
            assert caught.value is error
        else:
            frontier = validate_resume_frontier(source, plan, observation)
            assert frontier.source is source
            assert frontier.resumed_node_ids == ()
            assert frontier.checkpoints == ({"a": checkpoint} if error is None else {})
            assert frontier.closed_checkpoint_ids == frozenset()

    expected = (
        checks if failed_check is None else checks[: checks.index(failed_check) + 1]
    )
    if error_type is not TypeError:
        expected = [*expected, "read_review_checkpoint"]
    assert [call[0] for call in calls.mock_calls] == expected
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == contents


@pytest.mark.parametrize("gate", ["fenced", "completed", "dependencies", "closed"])
def test_checkpoint_gates_follow_identity_checks_and_skip_detailed_validation(
    tmp_path: Path, gate: str
) -> None:
    source, plan, checkpoint, observation = checkpoint_history(tmp_path)
    fences = frozenset({"a"}) if gate in {"fenced", "closed"} else frozenset()
    if gate in {"completed", "closed"}:
        descriptor = write_result(source.results_dir, "a-result.md", "complete")
        write_node_state(
            source.run_dir, make_node_state(source.manifest, "a", [descriptor])
        )
    if gate == "dependencies":
        edge = plan.dependency_graph[0].model_copy(
            update={"source_node": "b", "target_node": "a"}
        )
        plan = plan.model_copy(update={"dependency_graph": [edge]})
    elif gate == "closed":
        identity = {
            key: value
            for key, value in checkpoint.model_dump().items()
            if key in ClosedReviewCheckpoint.model_fields and key != "kind"
        }
        publish_review_checkpoint(
            source.run_dir,
            ClosedReviewCheckpoint(**identity, terminal_reason="no_progress"),
        )
        (source.run_dir / checkpoint.files[0].relative_path).unlink()

    calls = Mock()
    with ExitStack() as stack:
        for name in (
            "read_review_checkpoint",
            "checkpoint_matches_source",
            "validate_checkpoint_node",
            "_require_checkpoint_provenance",
            "require_checkpoint_dependencies",
            "require_checkpoint_project",
        ):
            spy = stack.enter_context(
                patch.object(validation, name, wraps=getattr(validation, name))
            )
            calls.attach_mock(spy, name)
        frontier = validate_resume_frontier(source, plan, observation, fences)

    assert frontier.checkpoint_node_ids == ()
    assert frontier.resumed_node_ids == (
        ("a",) if gate in {"completed", "closed"} else ()
    )
    assert frontier.closed_checkpoint_ids == (
        frozenset({"a"}) if gate == "closed" else frozenset()
    )
    assert [call[0] for call in calls.mock_calls] == [
        "read_review_checkpoint",
        "checkpoint_matches_source",
        "validate_checkpoint_node",
        "read_review_checkpoint",
    ]
