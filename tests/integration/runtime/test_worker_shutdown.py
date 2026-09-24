import subprocess
import sys
from pathlib import Path

import pytest

PROCESS_TIMEOUT_SECONDS = 30.0
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("scenario", ["workspace", "retry_reset"])
def test_blocked_worker_does_not_prevent_exit(scenario: str) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "tests.helpers.worker_shutdown_scenarios", scenario],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        stdout = (error.stdout or b"").decode(errors="replace")
        stderr = (error.stderr or b"").decode(errors="replace")
        pytest.fail(
            f"{scenario} exceeded the {PROCESS_TIMEOUT_SECONDS}s process timeout.\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}",
            pytrace=False,
        )
    assert result.returncode == 0, (
        f"{scenario} exited with status {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.stdout.strip() == "asyncio-run-returned", (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
