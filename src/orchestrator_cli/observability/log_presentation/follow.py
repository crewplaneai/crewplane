from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import sleep, time
from typing import Any, cast

from orchestrator_cli.architecture.contracts import (
    LogPresentationDescriptor,
    validate_log_presentation_descriptor,
)
from orchestrator_cli.observability.events.types import InvocationStatus
from orchestrator_cli.observability.log_presentation.formatters import format_log_file
from orchestrator_cli.observability.log_presentation.limits import (
    DEFAULT_FORMATTED_INSPECT_LINE_BUDGET,
    DEFAULT_LIMITS,
)

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
        log_path = Path(require_string(snapshot, "log_file"))
        status = status_from_snapshot(snapshot)
        line_budget = int(
            snapshot.get("line_budget") or DEFAULT_FORMATTED_INSPECT_LINE_BUDGET
        )
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


def read_snapshot(snapshot_path: Path) -> dict[str, Any]:
    value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("snapshot must be a JSON object")
    return value


def descriptor_from_snapshot(snapshot: dict[str, Any]) -> LogPresentationDescriptor:
    return validate_log_presentation_descriptor(
        {
            "format": require_string(snapshot, "log_presentation_format"),
            "profile": require_string(snapshot, "log_presentation_profile"),
        }
    )


def status_from_snapshot(snapshot: dict[str, Any]) -> InvocationStatus:
    value = snapshot.get("invocation_status", "running")
    if not isinstance(value, str) or value not in _VALID_STATUSES:
        return "running"
    return cast(InvocationStatus, value)


def require_string(snapshot: dict[str, Any], key: str) -> str:
    value = snapshot.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"snapshot missing {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
