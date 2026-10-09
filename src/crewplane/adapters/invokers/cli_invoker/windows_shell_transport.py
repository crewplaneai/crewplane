"""Execute an adapter-prepared shell command line without argv re-encoding."""

from __future__ import annotations

import base64
import subprocess
import sys


def main() -> int:
    shell, encoded = sys.argv[1:]
    command_line = base64.b64decode(encoded, validate=True).decode("utf-8")
    return subprocess.call(command_line, executable=shell, shell=False)


if __name__ == "__main__":
    raise SystemExit(main())
