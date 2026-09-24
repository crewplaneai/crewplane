"""Workspace setup command whose descendant announces its own readiness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    mode, directory = sys.argv[1:3]
    root = Path(directory)
    if mode == "child":
        temporary = root / "child-ready.tmp"
        temporary.write_text(
            json.dumps({"pid": os.getpid(), "process_group_id": os.getpgrp()}),
            encoding="utf-8",
        )
        temporary.replace(root / "child-ready.json")
        time.sleep(60)
        return
    if mode == "retry":
        counter = root / "setup-count.txt"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        Path("setup-marker.txt").write_text("ready")
        if count == 0:
            return
    temporary = root / "setup-pid.tmp"
    temporary.write_text(str(os.getpid()), encoding="utf-8")
    temporary.replace(root / "setup.pid")
    with subprocess.Popen([sys.executable, __file__, "child", directory]):
        time.sleep(60)


if __name__ == "__main__":
    main()
