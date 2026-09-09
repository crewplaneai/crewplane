"""Persisted run-summary header shared by rendering and recovery."""

_STATUS_PREFIX = "- Status: "


def run_summary_header_lines(workflow_name: str, run_id: str, status: str) -> list[str]:
    return [
        "# Run Summary",
        "",
        f"- Workflow: {workflow_name}",
        f"- Run ID: {run_id}",
        f"{_STATUS_PREFIX}{status}",
    ]


def run_summary_status_lines(summary: str) -> list[str]:
    return [line for line in summary.splitlines() if line.startswith(_STATUS_PREFIX)]
