"""Measure stream retention without pytest or plugin allocations."""

import json
import subprocess
import sys
import tracemalloc
from itertools import repeat
from pathlib import Path

from crewplane.runtime.agent.process.stream_capture import CapturedStream


def run_probe(script: Path, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """run() kills and reaps the direct child before raising TimeoutExpired."""
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def main() -> None:
    assert "pytest" not in sys.modules
    capture = CapturedStream(max_memory_bytes=64 * 1024)
    chunk = b"x" * 4096
    try:
        tracemalloc.start()
        for payload in repeat(chunk, (8 * 1024 * 1024) // len(chunk)):
            capture.write(payload)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        capture.close()
        assert capture.tail_bytes == b"x" * (64 * 1024)
        assert capture.path.stat().st_size == 8 * 1024 * 1024
        assert peak < 1024 * 1024, peak
    finally:
        tracemalloc.stop()
        capture.cleanup()
    assert not capture.path.exists()
    print(json.dumps({"peak_bytes": peak, "cleaned": True}))


if __name__ == "__main__":
    main()
