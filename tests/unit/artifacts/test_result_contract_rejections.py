from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from crewplane.architecture.ports.artifacts import StageTaskSpec
from crewplane.artifacts.results.findings import (
    FindingsExtractionError,
    FindingsSelection,
    extract_findings_content,
)
from crewplane.artifacts.results.review_loop_status import (
    ReviewLoopStatusError,
    resolve_review_loop_status,
    review_loop_status_path,
)
from crewplane.artifacts.results.selection import (
    latest_round_files,
    ordered_task_ids,
    parse_audit_round,
    parse_task_round,
)
from crewplane.artifacts.results.writer import ResultWriter


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size_bytes", -1, "non-negative"),
        ("round_num", 0, "positive"),
        ("audit_round_num", 0, "positive"),
        ("path", "executor_round2.txt", "point to a .md file"),
    ],
)
def test_review_status_rejects_invalid_output_descriptors(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    payload = _write_valid_status(tmp_path)
    assert resolve_review_loop_status("stage", tmp_path) is not None
    payload["canonical_executor_outputs"][0][field] = value
    review_loop_status_path(tmp_path).write_text(json.dumps(payload))

    with pytest.raises(ReviewLoopStatusError, match=message):
        resolve_review_loop_status("stage", tmp_path)


@pytest.mark.parametrize(
    "kind",
    [
        "unknown-task",
        "wrong-role",
        "different-round",
        "wrong-final-round",
        "final-exceeds-attempted",
    ],
)
def test_review_status_requires_consistent_producers_and_rounds(
    tmp_path: Path, kind: str
) -> None:
    payload = _write_valid_status(tmp_path)
    specs = ()
    if kind == "unknown-task":
        specs = (StageTaskSpec("other", "executor"),)
    elif kind == "wrong-role":
        specs = (StageTaskSpec("executor", "reviewer"),)
    elif kind == "different-round":
        reviewer = payload["reviewer_outputs"][0]
        reviewer["path"] = "reviewer_round1.md"
        reviewer["round_num"] = 1
        (tmp_path / "reviewer_round1.md").write_bytes(b"reviewer")
    elif kind == "wrong-final-round":
        payload["final_local_round_num"] = 1
    else:
        payload["final_local_round_num"] = 3
    review_loop_status_path(tmp_path).write_text(json.dumps(payload))

    with pytest.raises(
        ReviewLoopStatusError,
        match="unknown task|unexpected producer role|one audit|does not match|cannot exceed",
    ):
        resolve_review_loop_status("stage", tmp_path, specs)


def test_dangling_review_status_directory_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "review-state").symlink_to(
        tmp_path / "missing", target_is_directory=True
    )

    with pytest.raises(ReviewLoopStatusError, match="directory must not be a symlink"):
        resolve_review_loop_status("stage", tmp_path)


def test_latest_round_selection_uses_numeric_audit_order_and_preserves_task_order(
    tmp_path: Path,
) -> None:
    for audit, rounds in [(10, [3, 1]), (2, [9])]:
        directory = tmp_path / f"review-audit-round-{audit}"
        directory.mkdir()
        for round_num in rounds:
            (directory / f"executor_round{round_num}.md").write_text(
                f"audit {audit} round {round_num}"
            )
    (tmp_path / "executor_round99.md").write_text("stale root output")

    selected = latest_round_files(tmp_path)

    assert selected == {
        "executor": tmp_path / "review-audit-round-10" / "executor_round3.md"
    }
    selected["z-extra"] = tmp_path / "extra.md"
    specs = (
        StageTaskSpec("missing", "executor"),
        StageTaskSpec("executor", "executor"),
        StageTaskSpec("executor", "executor"),
    )
    assert ordered_task_ids(selected, specs) == ["executor", "z-extra"]


def test_legacy_round_names_with_non_integer_suffix_remain_plain_task_names() -> None:
    assert parse_task_round("executor_rounddraft") == ("executor_rounddraft", 0)
    assert parse_audit_round("review-audit-round-draft") == 0


def test_findings_without_task_metadata_apply_to_all_outputs_and_reject_empty_blocks(
    tmp_path: Path,
) -> None:
    selection = FindingsSelection.from_stage((), findings_enabled=True)
    assert selection.should_extract("executor", "output")
    assert selection.should_extract("custom-task", "output")
    with pytest.raises(FindingsExtractionError, match="must not be empty"):
        extract_findings_content(
            "<!-- findings -->\n \n<!-- /findings -->", tmp_path / "output.md"
        )


@pytest.mark.parametrize(
    "kind",
    ["missing-stage", "missing-findings-locator", "input-findings", "disabled-warning"],
)
def test_result_finalization_handles_absent_stages_and_findings_boundaries(
    tmp_path: Path, kind: str
) -> None:
    result = tmp_path / "result.md"
    findings = tmp_path / "findings.md"
    writer = ResultWriter(
        lambda name: tmp_path / f"{name}-result.md",
        lambda name: tmp_path / f"{name}-findings.md",
        kind != "disabled-warning",
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    if kind == "missing-stage":
        finalized = writer.finalize_at("stage", None, result, None)
        assert finalized.included_outputs == ()
        assert finalized.warnings
        assert not result.exists()
    elif kind == "missing-findings-locator":
        with pytest.raises(ValueError, match="findings"):
            writer.finalize_at("stage", stage, result, None, findings_enabled=True)
        assert not result.exists()
    elif kind == "input-findings":
        (stage / "input.md").write_text("input")
        with pytest.raises(ValueError, match="findings"):
            writer.finalize_at("stage", stage, result, findings, findings_enabled=True)
        assert not result.exists()
    else:
        (stage / "executor.md").write_text("")
        finalized = writer.finalize_at("stage", stage, result, None)
        assert finalized.warnings == ()
        assert finalized.skipped_empty_outputs == (stage / "executor.md",)


def _write_valid_status(stage: Path) -> dict[str, object]:
    entries = []
    for task in ("executor", "reviewer"):
        content = task.encode()
        filename = f"{task}_round2.md"
        (stage / filename).write_bytes(content)
        entries.append(
            {
                "task_id": task,
                "provider": task,
                "role": task,
                "path": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "audit_round_num": None,
                "round_num": 2,
            }
        )
    payload = {
        "node_id": "stage",
        "executed_audit_rounds": 1,
        "attempted_local_round_num": 2,
        "final_local_round_num": 2,
        "invalid_candidate_round_count": 0,
        "no_progress_round_count": 0,
        "artifact_drift_warning_count": 0,
        "consensus_reached": True,
        "continued_after_consensus_exhaustion": False,
        "canonical_executor_outputs": [entries[0]],
        "reviewer_outputs": [entries[1]],
    }
    path = review_loop_status_path(stage)
    path.parent.mkdir()
    path.write_text(json.dumps(payload))
    return payload
