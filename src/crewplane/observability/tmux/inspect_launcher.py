from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from crewplane.architecture.contracts import validate_log_presentation_descriptor
from crewplane.observability.tmux.inspect_snapshot import read_inspect_snapshot
from crewplane.observability.tmux.snapshot_types import (
    SelectedInvocationSnapshot,
    require_snapshot_string,
)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    snapshot = read_inspect_snapshot(args.snapshot)
    if snapshot is None:
        print("Inspect snapshot unavailable.", flush=True)
        return 1
    try:
        if snapshot["inspect_view"] == "formatted":
            exec_formatted(snapshot, args.snapshot)
        exec_raw(snapshot)
    except Exception as exc:
        print(f"Inspect launch failed: {exc.__class__.__name__}", flush=True)
        return 1
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch compact tmux log inspect.")
    parser.add_argument("--snapshot", type=Path, required=True)
    return parser.parse_args(argv)


def exec_raw(snapshot: SelectedInvocationSnapshot) -> None:
    log_path = require_snapshot_string(snapshot, "log_file")
    os.execvp("tail", ["tail", "-n", "+1", "-F", "--", log_path])


def exec_formatted(snapshot: SelectedInvocationSnapshot, snapshot_path: Path) -> None:
    validate_log_presentation_descriptor(
        {
            "format": require_snapshot_string(snapshot, "log_presentation_format"),
            "profile": require_snapshot_string(snapshot, "log_presentation_profile"),
        }
    )
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "crewplane.observability.log_presentation.follow",
            "--snapshot",
            str(snapshot_path),
        ],
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
