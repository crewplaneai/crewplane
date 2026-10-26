"""Receipt-synchronized foreground and inherited-pipe command descendants."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def receipt(root: Path, name: str) -> None:
    temporary = root / f"{name}.pending"
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "group": os.getpgrp(), "session": os.getsid(0)})
    )
    temporary.replace(root / f"{name}.json")


def wait_for_receipt(root: Path, name: str) -> None:
    deadline = time.monotonic() + 10
    while not (root / f"{name}.json").is_file():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Missing {name} receipt")
        time.sleep(0.01)


def main() -> None:
    mode, directory = sys.argv[1:]
    root = Path(directory)
    if mode in {"child", "grandchild"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode not in {"grandchild", "single"}:
        descendant = "grandchild" if mode == "child" else "child"
        subprocess.Popen([sys.executable, __file__, descendant, directory])
        wait_for_receipt(root, descendant)
    receipt(root, "parent" if mode.startswith("parent") else mode)
    if mode == "parent-exit":
        print("foreground parent exited")
        return
    signal.pause()


if __name__ == "__main__":
    main()
