"""Small real pytest projects for exercising the repository launcher."""

import os
import shutil
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from tests.helpers.fixture_files import copy_fixture

ROOT = Path(__file__).resolve().parents[2]


def runner_project(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir()
    for filename in (
        "run_tests.py",
        "test_process.py",
        "test_process_gate.py",
        "check_coverage.py",
    ):
        shutil.copyfile(ROOT / "scripts" / filename, scripts / filename)
    (root / "uv.lock").write_text("test-owned lockfile\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["."]\n'
        'strict_markers = true\nmarkers = ["race: controlled concurrency regression"]\n'
        'addopts = ["--disable-plugin-autoload", "--import-mode=importlib"]\n'
        '[tool.coverage.run]\npatch = ["subprocess"]\n',
        encoding="utf-8",
    )
    copy_fixture("processes/sleeping_process.py", root / "sleeping_process.py")
    return scripts / "run_tests.py"


def runner_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COVERAGE_", "COV_CORE_"))
    }


def run_fixture(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(script),
        "--profile",
        "nightly",
        "--hash-seed",
        "42",
        *args,
    ]
    with subprocess.Popen(
        command,
        cwd=script.parent.parent,
        env=runner_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired as error:
            with suppress(ProcessLookupError):
                process.send_signal(
                    signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
                )
            try:
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                kill_active_process_groups(script.parent.parent)
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
            error.output, error.stderr = stdout, stderr
            raise
        finally:
            if process.poll() is None:
                kill_active_process_groups(script.parent.parent)
                process.kill()
                process.wait(timeout=5)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def kill_active_process_groups(root: Path) -> None:
    if os.name != "posix":
        return
    for receipt in (root / "test-results").glob("*/*.active-pid"):
        with suppress(FileNotFoundError):
            group_id = int(receipt.read_text(encoding="utf-8"))
            assert group_id != os.getpgrp(), "Refusing to kill the pytest process group"
            with suppress(ProcessLookupError):
                os.killpg(group_id, signal.SIGKILL)
            receipt.unlink(missing_ok=True)
