from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from crewplane.architecture.contracts import EventType, WorkflowEventType
from crewplane.artifacts.locks import (
    acquire_same_context_lock,
)
from crewplane.artifacts.locks.manifest import (
    LockRunMetadata,
    TerminalRecoveryIntent,
    finalize_stale_running_run,
)
from crewplane.observability.events import (
    format_execution_event_log_line,
    workflow_event,
)
from tests.helpers.resume import (
    WORKFLOW_IDENTITY,
    WORKFLOW_NAME,
    WORKFLOW_SIGNATURE,
    make_run_manifest,
    write_run_manifest,
)
from tests.helpers.resume_locks import (
    FakeProcessInspector,
)


@pytest.mark.parametrize("field", ["workflow_name", "run_id"])
@pytest.mark.parametrize("separator", ["", "\n", "\r", "\u2028"])
def test_terminal_summary_rejects_identity_spanning_header_lines(
    tmp_path: Path, field: str, separator: str
) -> None:
    manifest = make_run_manifest("source", "workflow--source", status="running")
    manifest = manifest.model_copy(update={field: f"first{separator}second"})
    summary = (
        "# Run Summary\n\n"
        f"- Workflow: {manifest.workflow_name}\n"
        f"- Run ID: {manifest.run_id}\n"
        "- Status: succeeded\n"
    )

    manifest_path = write_run_manifest(tmp_path, manifest)
    _write_terminal_views(tmp_path, "succeeded", None)
    logs_dir = manifest_path.parent.parent / "logs"
    (logs_dir / "summary.md").write_text(summary, encoding="utf-8")
    event_path = logs_dir / "events.ndjson"
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event.update(workflow_name=manifest.workflow_name, run_id=manifest.run_id)
    event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

    finalize_stale_running_run(
        tmp_path,
        LockRunMetadata(
            run_id=manifest.run_id,
            run_key_name=manifest.run_key_name,
            workflow_identity=manifest.workflow_identity,
            workflow_signature=manifest.workflow_signature,
            terminal_recovery=TerminalRecoveryIntent(
                phase="outcome_selected", status="succeeded"
            ),
        ),
    )

    recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert recovered["status"] == ("cancelled" if separator else "succeeded")
    assert recovered.get("cancel_reason") == (
        "stale_lock_recovered" if separator else None
    )


def _write_terminal_views(
    state_dir: Path,
    status: Literal["succeeded", "failed", "cancelled"],
    reason: str | None,
    event_count: int = 1,
    summary_status: Literal["succeeded", "failed", "cancelled"] | None = None,
) -> None:
    event_type_by_status: dict[
        Literal["succeeded", "failed", "cancelled"],
        WorkflowEventType,
    ] = {
        "succeeded": EventType.WORKFLOW_FINISHED,
        "failed": EventType.WORKFLOW_FAILED,
        "cancelled": EventType.WORKFLOW_CANCELLED,
    }
    event = workflow_event(
        event_type_by_status[status],
        workflow_name=WORKFLOW_NAME,
        run_id="source",
        error=reason,
    )
    logs_dir = state_dir / "execution-stages" / "workflow--source" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "events.ndjson").write_text(
        format_execution_event_log_line(event) * event_count,
        encoding="utf-8",
    )
    (logs_dir / "summary.md").write_text(
        "# Run Summary\n\n"
        f"- Workflow: {WORKFLOW_NAME}\n"
        "- Run ID: source\n"
        f"- Status: {summary_status or status}\n",
        encoding="utf-8",
    )


def test_stale_lock_finalizes_running_manifest_as_cancelled(tmp_path) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
    finally:
        lock.release()


@pytest.mark.parametrize(
    ("status", "reason", "reason_field"),
    [
        ("succeeded", None, None),
        ("failed", "branch export failed", "failure_message"),
        ("cancelled", "external_cancellation", "cancel_reason"),
    ],
)
def test_stale_lock_replays_recorded_terminal_recovery(
    tmp_path,
    status: Literal["succeeded", "failed", "cancelled"],
    reason: str | None,
    reason_field: str | None,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    stale.record_terminal_recovery("outcome_selected", status, reason)
    stale.record_terminal_recovery("terminal_views_published", status, reason)
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == status
        assert manifest.get("failure_message") == (
            reason if reason_field == "failure_message" else None
        )
        assert manifest.get("cancel_reason") == (
            reason if reason_field == "cancel_reason" else None
        )
    finally:
        lock.release()


@pytest.mark.parametrize(
    ("event_count", "summary_status"),
    [
        pytest.param(0, "succeeded", id="missing-terminal-event"),
        pytest.param(1, "failed", id="mismatched-summary"),
    ],
)
def test_stale_lock_cancels_selected_outcome_without_matching_terminal_views(
    tmp_path,
    event_count: int,
    summary_status: Literal["succeeded", "failed", "cancelled"],
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    stale.record_terminal_recovery("outcome_selected", "succeeded", None)
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    _write_terminal_views(
        tmp_path,
        "succeeded",
        None,
        event_count=event_count,
        summary_status=summary_status,
    )

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
    finally:
        lock.release()


@pytest.mark.parametrize(
    ("status", "reason", "reason_field"),
    [
        ("succeeded", None, None),
        ("failed", "branch export failed", "failure_message"),
        ("cancelled", "external_cancellation", "cancel_reason"),
    ],
)
def test_stale_lock_replays_selected_outcome_with_matching_terminal_views(
    tmp_path,
    status: Literal["succeeded", "failed", "cancelled"],
    reason: str | None,
    reason_field: str | None,
) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    stale.record_terminal_recovery("outcome_selected", status, reason)
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    _write_terminal_views(tmp_path, status, reason)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == status
        assert manifest.get("failure_message") == (
            reason if reason_field == "failure_message" else None
        )
        assert manifest.get("cancel_reason") == (
            reason if reason_field == "cancel_reason" else None
        )
    finally:
        lock.release()


def test_stale_lock_rejects_duplicate_matching_terminal_events(tmp_path) -> None:
    stale = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(100, "old"),
    )
    stale.update_run("source", "workflow--source")
    stale.record_terminal_recovery("outcome_selected", "succeeded", None)
    manifest_path = write_run_manifest(
        tmp_path,
        make_run_manifest("source", "workflow--source", status="running"),
    )
    _write_terminal_views(tmp_path, "succeeded", None, event_count=2)

    lock = acquire_same_context_lock(
        tmp_path,
        WORKFLOW_NAME,
        WORKFLOW_IDENTITY,
        WORKFLOW_SIGNATURE,
        process_inspector=FakeProcessInspector(200, "new", live=False),
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "cancelled"
        assert manifest["cancel_reason"] == "stale_lock_recovered"
    finally:
        lock.release()
