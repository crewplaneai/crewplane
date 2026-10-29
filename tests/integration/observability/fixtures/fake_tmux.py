#!/usr/bin/env python3
"""Record tmux commands and supply deterministic pane IDs and dimensions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def pane_target(value: str) -> bool:
    return value.startswith("%") and value[1:].isdigit()


def validate_command(args: list[str]) -> None:
    """Accept only the command forms exercised by the compact dashboard tests."""
    match args:
        case [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            session,
            "-n",
            "dashboard",
            command,
        ] if session and command:
            return
        case [
            "split-window",
            "-d",
            "-h",
            "-p",
            "55",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            pane,
            command,
        ] if pane_target(pane) and command:
            return
        case [
            "has-session" | "kill-session" | "switch-client" | "attach",
            "-t",
            session,
        ] if session and not session.startswith("-"):
            return
        case [
            "display-message",
            "-p",
            "-t",
            pane,
            "#{pane_width}" | "#{pane_height}",
        ] if pane_target(pane):
            return
        case ["select-pane", "-t", pane] if pane_target(pane):
            return
        case ["send-keys", "-X", "-t", pane, "cancel"] if pane_target(pane):
            return
        case ["set-option", "-p", "-t", pane, "@crewplane_title", _] if pane_target(
            pane
        ):
            return
        case ["set-option", "-t", session, option, _] if session and option in {
            "status-left-length",
            "status-right-length",
            "mouse",
            "key-table",
            "pane-border-style",
            "pane-active-border-style",
            "status-left",
            "status-right",
        }:
            return
        case ["set-window-option", "-t", window, option, value] if (
            window
            and value
            and option in {"remain-on-exit", "pane-border-status", "pane-border-format"}
        ):
            return
        case ["bind-key", "-T", table, key, "if-shell", "-F", "1", command] if (
            table and key and command
        ):
            return
    raise ValueError(f"Unsupported tmux command or shape: {args!r}")


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["-L"]:
        if len(args) < 3 or not args[1] or args[1].startswith("-"):
            print("Unsupported tmux socket arguments", file=sys.stderr)
            return 1
        args = args[2:]
    log_path = Path(os.environ["FAKE_TMUX_LOG"])
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")
    try:
        validate_command(args)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    threshold = int(os.environ["FAKE_TMUX_BIND_ARG_THRESHOLD"])
    if args[0] == "bind-key" and any(len(arg) > threshold for arg in args):
        print("command too long", file=sys.stderr)
        return 1
    if args[0] == "new-session":
        print("%10")
    elif args[0] == "split-window":
        print("%20")
    elif args[0] == "display-message":
        target = args[3]
        if args[-1] == "#{pane_width}":
            print("100" if target == "%10" else "180")
        else:
            print("18" if target == "%10" else "30")
    return 0


if __name__ == "__main__":
    sys.exit(main())
