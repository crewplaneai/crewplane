import pytest

from crewplane.architecture.contracts.run_summary import run_summary_header_lines


@pytest.mark.parametrize(
    "status", ["pending", "running", "succeeded", "failed", "cancelled"]
)
def test_run_summary_header_preserves_persisted_format(status: str) -> None:
    assert run_summary_header_lines("Workflow café", "run-1", status) == [
        "# Run Summary",
        "",
        "- Workflow: Workflow café",
        "- Run ID: run-1",
        f"- Status: {status}",
    ]
