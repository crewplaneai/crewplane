from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console

from crewplane.cli.run.observability import print_end_of_run_summary
from crewplane.observability.run_summary.logger import PersistentRunLogger
from crewplane.observability.run_summary.models import RunSummary


def _summary(root: Path, status: str) -> RunSummary:
    return RunSummary(
        workflow_name="[bold]literal workflow[/bold]",
        run_id="example-run",
        workflow_status=status,
        review_consensus_unresolved=False,
        started_at="n/a",
        completed_at="n/a",
        elapsed_seconds=None,
        node_counts=None,
        invocation_count=None,
        invocation_timings=(),
        spend=None,
        provider_rollups=(),
        invocation_usages=(),
        omitted_invocation_usage_count=0,
        node_outcomes=(),
        workspace=None,
        issues=(),
        artifact_references=(),
        event_log_path=root / "events.ndjson",
        summary_path=root / "summary.md",
    )


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_final_summary_prints_once_using_passed_console(
    tmp_path: Path, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = StringIO()
    console = Console(file=stream, width=40)
    logger = Mock(spec=PersistentRunLogger)
    logger.last_summary = _summary(tmp_path, status)

    print_end_of_run_summary(console, logger)

    output = stream.getvalue()
    assert output.count("Run Summary:") == 1
    assert f"Execution: {status}" in output
    assert "[bold]literal workflow[/bold]" in output.replace("\n", "")
    assert "\x1b" not in output
    assert "Events:" in output
    logger.stop.assert_not_called()
    logger.refresh_summary.assert_not_called()


@pytest.mark.parametrize("missing_logger", [False, True])
def test_absent_logger_or_summary_prints_nothing(missing_logger: bool) -> None:
    stream = StringIO()
    console = Console(file=stream)
    logger = (
        None if missing_logger else Mock(spec=PersistentRunLogger, last_summary=None)
    )

    print_end_of_run_summary(console, logger)

    assert stream.getvalue() == ""


class _FailingStream(StringIO):
    def write(self, value: str) -> int:
        raise OSError(f"stream unavailable for {value!r}")


def test_console_write_error_is_not_swallowed(tmp_path: Path) -> None:
    console = Console(file=_FailingStream())
    logger = Mock(spec=PersistentRunLogger, last_summary=_summary(tmp_path, "failed"))

    with pytest.raises(OSError, match="stream unavailable"):
        print_end_of_run_summary(console, logger)
