from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from time import time
from typing import cast

from crewplane.architecture.contracts import validate_log_presentation_descriptor
from crewplane.observability.log_presentation.limits import (
    DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
)
from crewplane.observability.tmux.runtime_files import (
    RuntimeFiles,
    write_json_atomic,
)
from crewplane.observability.tmux.selection_control import (
    SelectionControlState,
)
from crewplane.observability.tmux.snapshot_types import (
    InspectInvocationSnapshot,
    InspectView,
    InvocationSnapshot,
    SelectedInvocationSnapshot,
    is_inspect_invocation_snapshot,
    is_selected_invocation_snapshot,
)

SNAPSHOT_SCHEMA_VERSION = 1
_INSPECT_SNAPSHOT_FIELDS = ("inspect_view", "line_budget", "created_at")


def read_snapshot(path: Path) -> InvocationSnapshot | None:
    value = read_snapshot_value(path)
    if value is None:
        return None
    if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    if any(field in value for field in _INSPECT_SNAPSHOT_FIELDS):
        if not is_inspect_invocation_snapshot(value):
            return None
        return value
    if not is_selected_invocation_snapshot(value):
        return None
    return value


def read_inspect_snapshot(path: Path) -> InspectInvocationSnapshot | None:
    value = read_snapshot_value(path)
    if value is None or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    if not is_inspect_invocation_snapshot(value):
        return None
    return value


def read_snapshot_value(path: Path) -> dict[object, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def selected_snapshot_is_current(
    selected: Mapping[str, object],
    control: SelectionControlState,
) -> bool:
    return (
        selected.get("selection_generation") == control.selection_generation
        and selected.get("requested_selected_index") == control.selected_index
    )


def has_valid_presentation(snapshot: Mapping[str, object]) -> bool:
    try:
        validate_log_presentation_descriptor(
            {
                "format": snapshot.get("log_presentation_format"),
                "profile": snapshot.get("log_presentation_profile"),
            }
        )
    except (TypeError, ValueError):
        return False
    return True


def write_inspect_snapshot(
    runtime_files: RuntimeFiles,
    selected: SelectedInvocationSnapshot,
    inspect_view: InspectView,
    line_budget: int = DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
) -> InspectInvocationSnapshot:
    snapshot = cast(InspectInvocationSnapshot, dict(selected))
    snapshot["inspect_view"] = inspect_view
    snapshot["line_budget"] = line_budget
    snapshot["created_at"] = time()
    write_json_atomic(runtime_files.inspect_invocation, snapshot)
    return snapshot
