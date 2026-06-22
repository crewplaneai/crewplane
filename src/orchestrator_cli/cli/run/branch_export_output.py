from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from rich.console import Console

BranchExportPayload = Mapping[str, object]


def print_branch_export_fulfillments(
    record_paths: Iterable[Path],
    console: Console,
) -> None:
    records = branch_export_records_from_paths(record_paths)
    print_branch_export_payloads(
        records,
        console,
        title="Branch export fulfillment",
        operation_label="operation",
    )


def print_branch_export_verifications(
    records: Iterable[BranchExportPayload],
    console: Console,
) -> None:
    print_branch_export_payloads(
        records,
        console,
        title="Branch export verification",
        operation_label="planned_operation",
    )


def branch_export_records_from_paths(
    record_paths: Iterable[Path],
) -> tuple[BranchExportPayload, ...]:
    return tuple(branch_export_record_from_path(path) for path in record_paths)


def branch_export_record_from_path(path: Path) -> BranchExportPayload:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _unavailable_branch_export_payload(path, str(exc))
    if not isinstance(payload, dict):
        return _unavailable_branch_export_payload(
            path,
            "record payload is not a JSON object",
        )
    return {**payload, "record_artifact": path.as_posix()}


def _unavailable_branch_export_payload(
    path: Path,
    failure_message: str,
) -> BranchExportPayload:
    return {
        "logical_worktree_name": "(unknown)",
        "status": "unavailable",
        "operation": "unavailable",
        "branch_name": None,
        "record_artifact": path.as_posix(),
        "failure_message": failure_message,
    }


def print_branch_export_payloads(
    records: Iterable[BranchExportPayload],
    console: Console,
    title: str,
    operation_label: str,
) -> None:
    for record in records:
        console.print(
            format_branch_export_payload(record, title, operation_label),
            markup=False,
        )


def format_branch_export_payload(
    record: BranchExportPayload,
    title: str,
    operation_label: str,
) -> str:
    parts = [
        f"{title}:",
        f"worktree={display_value(record.get('logical_worktree_name'))}",
        f"status={display_value(record.get('status'))}",
        f"{operation_label}={display_value(record.get('operation'))}",
        f"branch={display_value(record.get('branch_name'), empty='(none)')}",
    ]
    optional_fields = (
        ("result", short_hash(record.get("result_commit"))),
        ("skip_reason", text_value(record.get("skip_reason"))),
        ("record", text_value(record.get("record_artifact"))),
        ("failure", text_value(record.get("failure_message"))),
    )
    parts.extend(
        f"{name}={value}" for name, value in optional_fields if value is not None
    )
    return " ".join(parts)


def display_value(value: object, empty: str = "(unknown)") -> str:
    text = text_value(value)
    return empty if text is None else text


def text_value(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def short_hash(value: object) -> str | None:
    text = text_value(value)
    if text is None:
        return None
    if len(text) < 12:
        return text
    return text[:12]
