import signal
import subprocess
import sys
import time
from pathlib import Path


def test_block() -> None:
    signal.signal(getattr(signal, "SIGBREAK", signal.SIGINT), signal.SIG_IGN)
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("sleeping_process.py")),
            "--pid-file",
            "child.pid",
            "--ignore-interrupt",
        ]
    )
    time.sleep(60)
