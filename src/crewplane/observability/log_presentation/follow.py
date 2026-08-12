from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from time import sleep, time
from typing import cast

from crewplane.architecture.contracts import (
    LogPresentationDescriptor,
    validate_log_presentation_descriptor,
)
from crewplane.observability.events.types import InvocationStatus
from crewplane.observability.log_presentation.formatters import format_log_file
from crewplane.observability.log_presentation.limits import (
    DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
    DEFAULT_LIMITS,
)
from crewplane.observability.tmux.snapshot_types import require_snapshot_string

_VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "succeeded", "failed"}
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    while True:
        render_snapshot(args.snapshot)
        if args.once:
            return 0
        sleep(args.interval)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Follow a provider log using bounded formatted presentation."
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def render_snapshot(snapshot_path: Path) -> None:
    try:
        snapshot = read_snapshot(snapshot_path)
        descriptor = descriptor_from_snapshot(snapshot)
        log_path = Path(require_snapshot_string(snapshot, "log_file"))
        status = status_from_snapshot(snapshot)
        line_budget = snapshot_line_budget(snapshot)
        formatted = format_log_file(
            log_path=log_path,
            descriptor=descriptor,
            line_budget=line_budget,
            invocation_status=status,
            wall_time_now=time(),
            limits=DEFAULT_LIMITS,
        )
        lines = [notice.message for notice in formatted.notices]
        lines.extend(formatted.lines)
    except Exception as exc:
        lines = [f"Formatted log unavailable: {exc.__class__.__name__}"]

    print("\033[H\033[J", end="")
    if lines:
        print("\n".join(lines), flush=True)
    else:
        print("No formatted log output yet.", flush=True)


def read_snapshot(snapshot_path: Path) -> Mapping[str, object]:
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return cast(Mapping[str, object], value)


def descriptor_from_snapshot(
    snapshot: Mapping[str, object],
) -> LogPresentationDescriptor:
    return validate_log_presentation_descriptor(
        {
            "format": require_snapshot_string(snapshot, "log_presentation_format"),
            "profile": require_snapshot_string(snapshot, "log_presentation_profile"),
        }
    )


def snapshot_line_budget(snapshot: Mapping[str, object]) -> int:
    value = snapshot.get("line_budget")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_FORMATTED_INSPECT_LINE_BUDGET


def status_from_snapshot(snapshot: Mapping[str, object]) -> InvocationStatus:
    value = snapshot.get("invocation_status", "running")
    if not isinstance(value, str) or value not in _VALID_STATUSES:
        return "running"
    return cast(InvocationStatus, value)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
