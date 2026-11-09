from __future__ import annotations

import selectors
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from tests.helpers.processes import kill_process_group

WORKER = Path(__file__).parents[1] / "fixtures/memory/store_worker.py"


@contextmanager
def worker(root: Path, mode: str, title: str):
    process = subprocess.Popen(
        [sys.executable, "-u", str(WORKER), mode, str(root), title],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        kill_process_group(process.pid)
        process.communicate(timeout=5)


def line(process: subprocess.Popen[bytes]) -> str:
    assert process.stdout is not None
    result = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not result.endswith(b"\n"):
            assert selector.select(timeout=5), (
                "memory worker failed to reach its barrier"
            )
            byte = process.stdout.read(1)
            assert byte, "memory worker exited before its barrier"
            result.extend(byte)
    return result.decode().strip()


def release(process: subprocess.Popen[bytes]):
    assert process.stdin is not None
    process.stdin.write(b"continue\n")
    process.stdin.flush()
