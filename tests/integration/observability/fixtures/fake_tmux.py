#!/usr/bin/env python3
"""Record tmux commands and supply deterministic pane IDs and dimensions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    raw_args = sys.argv[1:]
    args = raw_args[2:] if len(raw_args) >= 2 and raw_args[0] == "-L" else raw_args
    log_path = Path(os.environ["FAKE_TMUX_LOG"])
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\n")

    threshold = int(os.environ["FAKE_TMUX_BIND_ARG_THRESHOLD"])
    if args and args[0] == "bind-key" and any(len(arg) > threshold for arg in args):
        print("command too long", file=sys.stderr)
        return 1

    if args and args[0] == "new-session" and "-P" in args:
        print("%10")
    elif args and args[0] == "split-window" and "-P" in args:
        print("%20")
    elif (
        args
        and args[0] == "display-message"
        and args[-1] in {"#{pane_width}", "#{pane_height}"}
    ):
        target = args[args.index("-t") + 1]
        if args[-1] == "#{pane_width}":
            print("100" if target == "%10" else "180")
        else:
            print("18" if target == "%10" else "30")
    return 0


if __name__ == "__main__":
    sys.exit(main())
