from pathlib import Path

import pytest

from crewplane.architecture.contracts import (
    EventType,
)
from crewplane.core.workflow.keywords import ProviderRole
from crewplane.observability.events import (
    ExecutionEventContext,
    format_execution_event_log_line,
    invocation_event,
    runtime_log_event,
)
from crewplane.runtime.execution.review_loop.drift import (
    capture as review_loop_drift_capture,
)
from crewplane.runtime.execution.review_loop.drift import (
    comparison as review_loop_drift_comparison,
)
from crewplane.runtime.execution.review_loop.drift import (
    detection as review_loop_drift_detection,
)
from crewplane.runtime.execution.review_loop.drift import (
    recovery as review_loop_drift_recovery,
)
from crewplane.runtime.execution.review_loop.drift import (
    reserved as review_loop_drift_reserved,
)
from crewplane.runtime.execution.review_loop.types import (
    ActivityWindow,
    DriftMonitoringWindow,
)
from tests.integration.runtime.execution.review_loop_drift_support import (
    make_drift_request,
)


@pytest.mark.parametrize("is_exclusive", [True, False])
def test_summary_drift_is_always_fatal(tmp_path: Path, is_exclusive: bool) -> None:
    request, output, _node_dir = make_drift_request(tmp_path)
    summary_path = output.get_run_summary_path()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_bytes(b"after")
    window = DriftMonitoringWindow(
        node_snapshot={},
        shared_reserved_snapshot=None,
        summary_before=b"before",
        event_log_before=None,
        activity_window=ActivityWindow(is_exclusive=is_exclusive, version=1),
    )

    drift = review_loop_drift_reserved.detect_summary_drift(request, window)

    assert drift.fatal_paths == (summary_path,)


@pytest.mark.parametrize("strict_expected_append", [True, False])
def test_event_log_destructive_drift_is_always_fatal(
    tmp_path: Path,
    strict_expected_append: bool,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"truncated",
        expected_append=b"",
        strict_expected_append=strict_expected_append,
    )

    assert drift.fatal_paths == (event_log_path,)


def test_event_log_restoration_preserves_registered_concurrent_appends(
    tmp_path: Path,
) -> None:
    request, output, node_dir = make_drift_request(tmp_path)
    publications = request.runtime_context.runtime_publications
    request.runtime_publications = publications
    event_log_path = output.get_run_event_log_path()
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    baseline = b"baseline\n"
    concurrent_append = b"concurrent runtime event\n"
    event_log_path.write_bytes(baseline)
    window = review_loop_drift_capture.capture_drift_monitoring_window(
        request.node.id,
        node_dir,
        output,
        telemetry=None,
        runtime_publications=publications,
    )
    with publications.event_publication("sibling", concurrent_append):
        event_log_path.write_bytes(baseline + concurrent_append)
    event_log_path.write_bytes(b"provider mutation")

    drift = review_loop_drift_detection.detect_provider_call_drift(
        request,
        window,
        event_log_capture=None,
        event_log_start_index=0,
    )
    review_loop_drift_recovery.restore_fatal_artifacts(
        request,
        window,
        drift.fatal_paths,
    )

    assert event_log_path in drift.fatal_paths
    assert event_log_path.read_bytes() == baseline + concurrent_append


def test_event_log_absent_before_after_no_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_is_not_fatal(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=None,
        after=None,
        expected_append=b"unexpected event\n",
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()


def test_event_log_absent_before_after_expected_append_must_match(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    expected_append = b"appended\\n"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=None,
        after=expected_append,
        expected_append=expected_append,
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_empty_creation_is_fatal_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"",
        expected_append=b"",
        strict_expected_append=True,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == (event_log_path,)


def test_event_log_creation_mismatch_is_ignored_when_not_strict(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=None,
        after=b"concurrent event\n",
        expected_append=b"expected event\n",
        strict_expected_append=False,
    )

    assert drift.warning_paths == ()
    assert drift.fatal_paths == ()


def test_event_log_append_mismatch_is_fatal_only_under_strict_append_check(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"

    strict_drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=True,
    )
    non_strict_drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=b"before\n",
        after=b"before\nunexpected\n",
        expected_append=b"expected\n",
        strict_expected_append=False,
    )

    assert strict_drift.fatal_paths == (event_log_path,)
    assert non_strict_drift.fatal_paths == ()


def test_registered_event_append_requires_this_invocation_attribution(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    expected_append = b"expected provider event\n"
    concurrent_append = b"concurrent provider event\n"

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=b"baseline\n",
        after=b"baseline\n" + concurrent_append,
        expected_append=expected_append,
        strict_expected_append=True,
        registered_append=concurrent_append,
        registered_owned_append=b"",
    )

    assert drift.fatal_paths == (event_log_path,)


def test_event_log_append_allows_ambient_runtime_warning(
    tmp_path: Path,
) -> None:
    event_log_path = tmp_path / "events.ndjson"
    started = format_execution_event_log_line(
        invocation_event(
            EventType.INVOCATION_STARTED,
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    finished = format_execution_event_log_line(
        invocation_event(
            EventType.INVOCATION_FINISHED,
            "workflow",
            "run-1",
            context=ExecutionEventContext(
                workflow_name="workflow",
                run_id="run-1",
                node_id="review.node",
                provider="exec",
                role=ProviderRole.EXECUTOR,
                task_id="exec_executor_0",
            ),
        )
    ).encode("utf-8")
    ambient_warning = format_execution_event_log_line(
        runtime_log_event(
            "workflow",
            "run-1",
            level="warning",
            message="tmux command timed out; live dashboard may be stale",
            operation="runtime_warning",
        )
    ).encode("utf-8")

    drift = review_loop_drift_comparison.detect_event_log_drift(
        event_log_path,
        before=b'{"event":"baseline"}\n',
        after=b'{"event":"baseline"}\n' + started + ambient_warning + finished,
        expected_append=started + finished,
        strict_expected_append=True,
    )

    assert drift.fatal_paths == ()
    assert drift.warning_paths == ()
