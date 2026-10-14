"""Wait until the owning Windows Job has attached before launching its child."""

import subprocess
import sys


def main() -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 1
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
