from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from time import time
from typing import Any, Literal

from orchestrator_cli.architecture.contracts import validate_log_presentation_descriptor
from orchestrator_cli.observability.log_presentation.limits import (
    DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
)
from orchestrator_cli.observability.tmux.runtime_files import (
    RuntimeFiles,
    write_json_atomic,
)
from orchestrator_cli.observability.tmux.selection_control import (
    SelectionControlState,
)

InspectView = Literal["raw", "formatted"]
SNAPSHOT_SCHEMA_VERSION = 1


def read_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    return value


def selected_snapshot_is_current(
    selected: Mapping[str, Any],
    control: SelectionControlState,
) -> bool:
    return (
        selected.get("selection_generation") == control.selection_generation
        and selected.get("requested_selected_index") == control.selected_index
    )


def has_valid_presentation(snapshot: Mapping[str, Any]) -> bool:
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
    selected: Mapping[str, Any],
    inspect_view: InspectView,
    line_budget: int = DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
) -> dict[str, Any]:
    snapshot = dict(selected)
    snapshot["inspect_view"] = inspect_view
    snapshot["line_budget"] = line_budget
    snapshot["created_at"] = time()
    write_json_atomic(runtime_files.inspect_invocation, snapshot)
    return snapshot
