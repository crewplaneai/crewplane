from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from crewplane.artifacts.results.review_loop_status import ReviewLoopStatusError
from crewplane.artifacts.results.writer import ResultWriter
from tests.unit.artifacts.test_review_loop_status import (
    INVALID_STATUS_CASES,
    StatusMutator,
    create_referenced_outputs,
    valid_status_payload,
    write_status,
)

StatusWriter = Callable[[Path], None]


def build_writer(result_file: Path, findings_file: Path) -> ResultWriter:
    def result_resolver(stage_name: str) -> Path:
        return result_file if stage_name else result_file

    def findings_resolver(stage_name: str) -> Path:
        return findings_file if stage_name else findings_file

    return ResultWriter(
        result_file_resolver=result_resolver,
        findings_file_resolver=findings_resolver,
        empty_output_warning_enabled=True,
    )


def preserve_existing_outputs(
    writer: ResultWriter,
    stage_dir: Path,
    result_file: Path,
    findings_file: Path,
) -> None:
    result_file.parent.mkdir(parents=True, exist_ok=True)
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text("existing result", encoding="utf-8")
    findings_file.write_text("existing findings", encoding="utf-8")

    with pytest.raises(
        ReviewLoopStatusError, match="^Invalid review-loop status artifact"
    ):
        writer.finalize_stage("stage", stage_dir, findings_enabled=True)

    assert result_file.read_text(encoding="utf-8") == "existing result"
    assert findings_file.read_text(encoding="utf-8") == "existing findings"


@pytest.mark.parametrize("case_name,mutator", INVALID_STATUS_CASES)
def test_invalid_status_does_not_overwrite_existing_outputs(
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
    writer = build_writer(tmp_path / "result.md", tmp_path / "findings.md")

    preserve_existing_outputs(
        writer, stage_dir, tmp_path / "result.md", tmp_path / "findings.md"
    )


@pytest.mark.parametrize(
    "status_writer",
    (
        lambda stage_dir: write_status(stage_dir, "{"),
        lambda stage_dir: write_status(stage_dir, "[]"),
    ),
)
def test_invalid_json_status_does_not_overwrite_existing_outputs(
    tmp_path: Path,
    status_writer: StatusWriter,
) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    status_writer(stage_dir)
    writer = build_writer(tmp_path / "result.md", tmp_path / "findings.md")

    preserve_existing_outputs(
        writer, stage_dir, tmp_path / "result.md", tmp_path / "findings.md"
    )


def make_invalid_status_writer(mutator: StatusMutator) -> StatusWriter:
    def write_invalid_status(stage_dir: Path) -> None:
        create_referenced_outputs(stage_dir)
        payload = valid_status_payload()
        mutator(payload, stage_dir)

    return write_invalid_status


INVALID_FINALIZER_STATUS_WRITERS: tuple[tuple[str, StatusWriter], ...] = (
    ("malformed-json", lambda stage_dir: write_status(stage_dir, "{")),
    ("non-object-json", lambda stage_dir: write_status(stage_dir, "[]")),
    *(
        (case_name, make_invalid_status_writer(mutator))
        for case_name, mutator in INVALID_STATUS_CASES
    ),
)


@pytest.mark.parametrize("case_name,status_writer", INVALID_FINALIZER_STATUS_WRITERS)
def test_invalid_status_does_not_create_outputs_when_absent(
    tmp_path: Path,
    case_name: str,
    status_writer: StatusWriter,
) -> None:
    assert case_name
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    status_writer(stage_dir)
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    writer = build_writer(result_file, findings_file)

    with pytest.raises(
        ReviewLoopStatusError, match="^Invalid review-loop status artifact"
    ):
        writer.finalize_stage("stage", stage_dir, findings_enabled=True)

    assert not result_file.exists()
    assert not findings_file.exists()


def test_valid_empty_status_produces_empty_selection_without_fallback(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (stage_dir / "older_round1.md").write_text(
        "should not be included", encoding="utf-8"
    )
    payload = valid_status_payload()
    payload["canonical_executor_outputs"] = []
    payload["reviewer_outputs"] = []
    write_status(stage_dir, payload)
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    writer = build_writer(result_file, findings_file)

    result = writer.finalize_stage("stage", stage_dir)

    assert result.included_outputs == ()
    assert result.findings_file is None
    assert "should not be included" not in result_file.read_text(encoding="utf-8")


def test_valid_status_selects_declared_outputs_only(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    create_referenced_outputs(stage_dir)
    (stage_dir / "z_lexical_latest_round99.md").write_text("stale", encoding="utf-8")
    write_status(stage_dir, valid_status_payload())
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    writer = build_writer(result_file, findings_file)

    result = writer.finalize_stage("stage", stage_dir)

    result_text = result_file.read_text(encoding="utf-8")
    assert tuple(path.name for path in result.included_outputs) == (
        "executor_round2.md",
        "reviewer_round1.md",
    )
    assert "executor" in result_text
    assert "reviewer" in result_text
    assert "stale" not in result_text
