from __future__ import annotations

from pathlib import Path

from crewplane.architecture.ports.artifacts import StageTaskSpec
from crewplane.artifacts.results.writer import ResultWriter
from crewplane.core.workflow.keywords import ProviderRole


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


def test_single_provider_result_uses_output_heading(tmp_path: Path) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (stage_dir / "alpha_executor_0_round1.md").write_text(
        "single provider output",
        encoding="utf-8",
    )
    result_file = tmp_path / "result.md"
    writer = build_writer(result_file, tmp_path / "findings.md")

    writer.finalize_stage(
        "build",
        stage_dir,
        task_specs=(
            StageTaskSpec(
                task_id="alpha_executor_0",
                role=ProviderRole.EXECUTOR,
                display_name="alpha (executor)",
            ),
        ),
    )

    result_text = result_file.read_text(encoding="utf-8")
    assert "## Output" in result_text
    assert "single provider output" in result_text
    assert "## alpha_executor_0" not in result_text
    assert "## alpha (executor)" not in result_text


def test_multi_provider_result_and_findings_use_display_headings(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (stage_dir / "alpha_executor_0_round1.md").write_text(
        "alpha output\n\n<!-- findings -->\n- alpha finding\n<!-- /findings -->",
        encoding="utf-8",
    )
    (stage_dir / "beta_executor_1_round1.md").write_text(
        "beta output\n\n<!-- findings -->\n- beta finding\n<!-- /findings -->",
        encoding="utf-8",
    )
    (stage_dir / "review_reviewer_0_round1.md").write_text(
        "reviewer output",
        encoding="utf-8",
    )
    result_file = tmp_path / "result.md"
    findings_file = tmp_path / "findings.md"
    writer = build_writer(result_file, findings_file)

    writer.finalize_stage(
        "review",
        stage_dir,
        findings_enabled=True,
        task_specs=(
            StageTaskSpec(
                task_id="alpha_executor_0",
                role=ProviderRole.EXECUTOR,
                display_name="alpha (executor)",
            ),
            StageTaskSpec(
                task_id="beta_executor_1",
                role=ProviderRole.EXECUTOR,
                display_name="beta (executor)",
            ),
            StageTaskSpec(
                task_id="review_reviewer_0",
                role=ProviderRole.REVIEWER,
                display_name="review (reviewer)",
            ),
        ),
    )

    result_text = result_file.read_text(encoding="utf-8")
    findings_text = findings_file.read_text(encoding="utf-8")
    assert "## alpha (executor)" in result_text
    assert "## beta (executor)" in result_text
    assert "## review (reviewer)" in result_text
    assert "## alpha_executor_0" not in result_text
    assert "## beta_executor_1" not in result_text
    assert "## review_reviewer_0" not in result_text
    assert "## alpha (executor)" in findings_text
    assert "## beta (executor)" in findings_text
    assert "## alpha_executor_0" not in findings_text
    assert "## beta_executor_1" not in findings_text
