from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.artifacts.results.review_loop_status import (
    ReviewLoopStatusError,
    resolve_review_loop_status,
)
from crewplane.core.workflow.keywords import ProviderRole
from tests.helpers.platforms import symlink_or_skip
from tests.unit.artifacts.review_loop_status_support import (
    INVALID_STATUS_CASES,
    StatusMutator,
    create_referenced_outputs,
    malformed_json,
    non_object_json,
    valid_status_payload,
    write_status,
)


def test_resolves_valid_status_with_outputs(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    create_referenced_outputs(stage_dir)
    write_status(stage_dir, valid_status_payload())

    resolved = resolve_review_loop_status("stage", stage_dir)

    assert resolved is not None
    assert resolved.canonical_executor_outputs[0].role is ProviderRole.EXECUTOR
    assert resolved.reviewer_outputs[0].role is ProviderRole.REVIEWER
    assert tuple(resolved.selected_output_files) == ("executor", "reviewer")
    assert resolved.selected_output_files["executor"].name == "executor_round2.md"


@pytest.mark.parametrize(
    "field, value",
    [
        ("stop_reason", "unknown"),
        ("stop_reason", []),
        ("consecutive_no_progress_round_count", True),
        ("consecutive_no_progress_round_count", -1),
        ("continued_after_stop", "true"),
    ],
)
def test_rejects_invalid_stop_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    payload = valid_status_payload()
    payload[field] = value
    create_referenced_outputs(tmp_path)
    write_status(tmp_path, payload)

    with pytest.raises(ReviewLoopStatusError, match=field):
        resolve_review_loop_status("stage", tmp_path)


def test_rejects_reviewer_output_retained_from_a_prior_audit(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "stage"
    prior_audit_dir = stage_dir / "review-audit-round-1"
    final_audit_dir = stage_dir / "review-audit-round-2"
    prior_audit_dir.mkdir(parents=True)
    final_audit_dir.mkdir()
    (prior_audit_dir / "reviewer_round3.md").write_text("reviewer", encoding="utf-8")
    (final_audit_dir / "executor_round1.md").write_text("executor", encoding="utf-8")
    payload = valid_status_payload()
    payload["executed_audit_rounds"] = 2
    payload["final_local_round_num"] = 2
    canonical_outputs = payload["canonical_executor_outputs"]
    reviewer_outputs = payload["reviewer_outputs"]
    assert isinstance(canonical_outputs, list)
    assert isinstance(reviewer_outputs, list)
    canonical_outputs[0]["path"] = "review-audit-round-2/executor_round1.md"
    reviewer_outputs[0]["path"] = "review-audit-round-1/reviewer_round3.md"
    write_status(stage_dir, payload)

    with pytest.raises(ReviewLoopStatusError):
        resolve_review_loop_status("stage", stage_dir)


def test_rejects_output_bytes_changed_after_status_publication(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    create_referenced_outputs(stage_dir)
    write_status(stage_dir, valid_status_payload())
    output_path = stage_dir / "reviewer_round2.md"
    output_path.write_text("replaced", encoding="utf-8")

    with pytest.raises(ReviewLoopStatusError, match="bytes do not match"):
        resolve_review_loop_status("stage", stage_dir)


def test_rejects_hardlinked_output_inside_stage(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    source_path = stage_dir / "source.md"
    output_path = stage_dir / "executor_round2.md"
    source_path.write_text("executor", encoding="utf-8")
    try:
        os.link(source_path, output_path)
    except OSError as exc:
        pytest.skip(f"hardlink creation is unavailable: {exc}")
    write_status(stage_dir, valid_status_payload())

    with pytest.raises(ReviewLoopStatusError, match="safe regular file"):
        resolve_review_loop_status("stage", stage_dir)


def test_rejects_symlinked_status_artifact(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    outside_status = tmp_path / "status.json"
    outside_status.write_text(json.dumps(valid_status_payload()), encoding="utf-8")
    status_dir = stage_dir / "review-state"
    status_dir.mkdir()
    status_path = status_dir / "review-loop-status.json"
    try:
        symlink_or_skip(status_path, outside_status)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ReviewLoopStatusError, match="safe regular file"):
        resolve_review_loop_status("stage", stage_dir)


def test_resolves_valid_empty_status_without_fallback(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    payload = valid_status_payload()
    payload["canonical_executor_outputs"] = []
    payload["reviewer_outputs"] = []
    write_status(stage_dir, payload)

    resolved = resolve_review_loop_status("stage", stage_dir)

    assert resolved is not None
    assert resolved.selected_output_files == {}


@pytest.mark.parametrize("case_name,mutator", INVALID_STATUS_CASES)
def test_rejects_invalid_status_payloads(
    tmp_path: Path,
    case_name: str,
    mutator: StatusMutator,
) -> None:
    assert case_name
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    create_referenced_outputs(stage_dir)
    payload = valid_status_payload()
    mutator(payload, stage_dir)

    with pytest.raises(
        ReviewLoopStatusError, match="^Invalid review-loop status artifact"
    ):
        resolve_review_loop_status("stage", stage_dir)


@pytest.mark.parametrize("writer", (malformed_json, non_object_json))
def test_rejects_invalid_status_json(
    tmp_path: Path, writer: Callable[[Path], None]
) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    writer(stage_dir)

    with pytest.raises(
        ReviewLoopStatusError, match="^Invalid review-loop status artifact"
    ):
        resolve_review_loop_status("stage", stage_dir)


@pytest.mark.parametrize("value", [None, False, True, -1, 1.0, "1", 0, 10**30])
def test_status_counter_boundary(tmp_path: Path, value: object) -> None:
    create_referenced_outputs(tmp_path)
    payload = valid_status_payload()
    payload["no_progress_round_count"] = value
    write_status(tmp_path, payload)
    if type(value) is int and value >= 0:
        assert resolve_review_loop_status("stage", tmp_path) is not None
    else:
        with pytest.raises(
            ReviewLoopStatusError,
            match="no_progress_round_count must be a non-negative integer",
        ):
            resolve_review_loop_status("stage", tmp_path)


@pytest.mark.parametrize("value", [None, False, True, -1, 1.0, "1", 0])
def test_status_round_boundary_rejects_nonpositive_values(
    tmp_path: Path, value: object
) -> None:
    create_referenced_outputs(tmp_path)
    payload = valid_status_payload()
    payload["canonical_executor_outputs"][0]["round_num"] = value
    write_status(tmp_path, payload)
    with pytest.raises(
        ReviewLoopStatusError, match=r"round_num must be (non-negative|positive)"
    ):
        resolve_review_loop_status("stage", tmp_path)
