from __future__ import annotations

import io
import json
from pathlib import Path

from rich.console import Console

from crewplane.cli.run.branch_export_output import (
    branch_export_records_from_paths,
    print_branch_export_fulfillments,
    print_branch_export_verifications,
)


def test_print_branch_export_fulfillment_records(tmp_path: Path) -> None:
    record_path = tmp_path / "workspace-exports" / "primary.json"
    record_path.parent.mkdir()
    record_path.write_text(
        json.dumps(
            {
                "logical_worktree_name": "primary",
                "status": "fulfilled",
                "operation": "created",
                "branch_name": "feature/exported",
                "result_commit": "abcdef1234567890",
            }
        ),
        encoding="utf-8",
    )
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )

    print_branch_export_fulfillments((record_path,), console)

    output = stream.getvalue()
    assert "Branch export fulfillment:" in output
    assert "worktree=primary" in output
    assert "status=fulfilled" in output
    assert "operation=created" in output
    assert "branch=feature/exported" in output
    assert "result=abcdef123456" in output
    assert f"record={record_path.as_posix()}" in output


def test_print_branch_export_fulfillment_includes_skipped_reason(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "workspace-exports" / "scratch.json"
    record_path.parent.mkdir()
    record_path.write_text(
        json.dumps(
            {
                "logical_worktree_name": "scratch",
                "status": "skipped",
                "operation": "skipped",
                "branch_name": None,
                "skip_reason": "create_branch_false",
            }
        ),
        encoding="utf-8",
    )
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )

    print_branch_export_fulfillments((record_path,), console)

    output = stream.getvalue()
    assert "status=skipped" in output
    assert "operation=skipped" in output
    assert "branch=(none)" in output
    assert "skip_reason=create_branch_false" in output


def test_print_branch_export_verification_uses_planned_operation_label() -> None:
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )

    print_branch_export_verifications(
        (
            {
                "logical_worktree_name": "primary",
                "status": "fulfilled",
                "operation": "verified_existing",
                "branch_name": "feature/existing",
            },
        ),
        console,
    )

    output = stream.getvalue()
    assert "Branch export verification:" in output
    assert "planned_operation=verified_existing" in output


def test_print_branch_export_fulfillment_includes_failure_message(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "workspace-exports" / "primary.json"
    record_path.parent.mkdir()
    record_path.write_text(
        json.dumps(
            {
                "logical_worktree_name": "primary",
                "status": "failed_verification",
                "operation": "failed_verification",
                "branch_name": "feature/exported",
                "failure_message": "refuses to overwrite existing branch",
            }
        ),
        encoding="utf-8",
    )
    stream = io.StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=240,
    )

    print_branch_export_fulfillments((record_path,), console)

    output = stream.getvalue()
    assert "status=failed_verification" in output
    assert "failure=refuses to overwrite existing branch" in output


def test_branch_export_record_load_failure_is_printable(tmp_path: Path) -> None:
    record_path = tmp_path / "workspace-exports" / "broken.json"
    record_path.parent.mkdir()
    record_path.write_text("not-json", encoding="utf-8")

    records = branch_export_records_from_paths((record_path,))

    assert len(records) == 1
    assert records[0]["status"] == "unavailable"
    assert records[0]["operation"] == "unavailable"
    assert records[0]["record_artifact"] == record_path.as_posix()
    assert "failure_message" in records[0]


def test_branch_export_record_non_object_payload_is_printable(tmp_path: Path) -> None:
    record_path = tmp_path / "workspace-exports" / "broken.json"
    record_path.parent.mkdir()
    record_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    records = branch_export_records_from_paths((record_path,))

    assert len(records) == 1
    assert records[0]["status"] == "unavailable"
    assert records[0]["operation"] == "unavailable"
    assert records[0]["record_artifact"] == record_path.as_posix()
    assert records[0]["failure_message"] == "record payload is not a JSON object"
