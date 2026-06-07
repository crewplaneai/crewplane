from __future__ import annotations

from pathlib import Path
from typing import Any


def assert_case_output(case_data: dict[str, Any], rendered: str) -> None:
    for fragment in case_data.get("expected_fragments", ()):
        assert fragment in rendered
    for fragment in case_data.get("unexpected_fragments", ()):
        assert fragment not in rendered


def test_render_dag_summary_matches_proposal_topologies(
    tmp_path: Path,
    run_visualization_case,
    dag_render_topology_case: dict[str, Any],
) -> None:
    run_result = run_visualization_case(tmp_path, dag_render_topology_case)
    assert_case_output(dag_render_topology_case, run_result.rendered)
    assert "workflow_finished" in run_result.event_log_path.read_text(encoding="utf-8")


def test_render_dag_summary_matches_proposal_status_cases(
    tmp_path: Path,
    run_visualization_case,
    dag_render_status_case: dict[str, Any],
) -> None:
    run_result = run_visualization_case(tmp_path, dag_render_status_case)
    assert_case_output(dag_render_status_case, run_result.rendered)
    summary_text = run_result.summary_path.read_text(encoding="utf-8")
    expected_status = (
        "failed" if dag_render_status_case.get("expect_error") else "succeeded"
    )
    assert f"- Status: {expected_status}" in summary_text
