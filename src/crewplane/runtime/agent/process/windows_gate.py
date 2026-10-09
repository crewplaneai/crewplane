"""Execute a prepared argv only after the parent releases its binary gate."""

from __future__ import annotations

import os
import subprocess
import sys

GATE_BYTE = b"\x01"


def main() -> int:
    import msvcrt

    vars(msvcrt)["setmode"](sys.stdin.fileno(), vars(os)["O_BINARY"])
    # A buffered read can consume prompt bytes belonging to the provider.
    if os.read(sys.stdin.fileno(), 1) != GATE_BYTE:
        return 125
    command = sys.argv[1:]
    if not command:
        return 125
    try:
        process = subprocess.Popen(command, shell=False)
        return process.wait()
    except OSError as exc:
        print(f"Provider launch failed: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
