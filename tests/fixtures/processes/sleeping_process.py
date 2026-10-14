"""Publish process receipts and wait for the owning test to terminate us."""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid-file", type=Path)
    parser.add_argument("--child-pid-file", type=Path)
    parser.add_argument("--ignore-interrupt", action="store_true")
    args = parser.parse_args()
    if args.ignore_interrupt:
        signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), signal.SIG_IGN)
    if args.pid_file is not None:
        args.pid_file.write_text(str(os.getpid()), encoding="utf-8")
    if args.child_pid_file is not None:
        subprocess.Popen(
            [sys.executable, __file__, "--pid-file", str(args.child_pid_file)],
            start_new_session=True,
        )
    time.sleep(60)


if __name__ == "__main__":
    main()
