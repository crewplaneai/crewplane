from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from orchestrator_cli.architecture.contracts import validate_log_presentation_descriptor
from orchestrator_cli.observability.tmux.inspect_snapshot import read_snapshot


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    snapshot = read_snapshot(args.snapshot)
    if snapshot is None:
        print("Inspect snapshot unavailable.", flush=True)
        return 1
    try:
        if snapshot.get("inspect_view") == "formatted":
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


def exec_raw(snapshot: dict[str, Any]) -> None:
    log_path = require_string(snapshot, "log_file")
    os.execvp("tail", ["tail", "-n", "+1", "-F", "--", log_path])


def exec_formatted(snapshot: dict[str, Any], snapshot_path: Path) -> None:
    validate_log_presentation_descriptor(
        {
            "format": require_string(snapshot, "log_presentation_format"),
            "profile": require_string(snapshot, "log_presentation_profile"),
        }
    )
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "orchestrator_cli.observability.log_presentation.follow",
            "--snapshot",
            str(snapshot_path),
        ],
    )


def require_string(snapshot: dict[str, Any], key: str) -> str:
    value = snapshot.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"snapshot missing {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
