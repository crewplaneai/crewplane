"""Small real pytest projects for exercising the repository launcher."""

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from tests.helpers.execution_environment import (
    build_execution_environment,
    validation_controls,
    validation_tool_path,
)
from tests.helpers.fixture_files import copy_fixture

ROOT = Path(__file__).resolve().parents[2]


def runner_project(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir()
    for filename in (
        "run_tests.py",
        "validation_policy.py",
        "test_process.py",
        "test_process_gate.py",
        "check_coverage.py",
    ):
        shutil.copyfile(ROOT / "scripts" / filename, scripts / filename)
    helpers = root / "tests/helpers"
    helpers.mkdir(parents=True)
    for relative in ("tests/__init__.py", "tests/helpers/__init__.py"):
        shutil.copyfile(ROOT / relative, root / relative)
    shutil.copyfile(
        ROOT / "tests/helpers/execution_environment.py",
        helpers / "execution_environment.py",
    )
    copy_fixture("test_runner/scenario_runner.py", scripts / "scenario_runner.py")
    (root / "uv.lock").write_text("test-owned lockfile\n", encoding="utf-8")
    policy = (
        (ROOT / "pyproject.toml").read_text(encoding="utf-8").split("[tool.ruff]", 1)[0]
    )
    (root / "pyproject.toml").write_text(
        policy + '[tool.pytest.ini_options]\ntestpaths = ["."]\n'
        'strict_markers = true\nmarkers = ["race: controlled concurrency regression"]\n'
        'addopts = ["--disable-plugin-autoload", "--import-mode=importlib"]\n'
        '[tool.coverage.run]\npatch = ["subprocess"]\n',
        encoding="utf-8",
    )
    copy_fixture("processes/sleeping_process.py", root / "sleeping_process.py")
    return scripts / "run_tests.py"


def runner_environment(
    root: Path, pollution: Mapping[str, str] | None = None
) -> dict[str, str]:
    environment = build_execution_environment(
        root / "launcher-environment",
        validation_tool_path(os.environ),
        validation_controls(os.environ),
    )
    environment.update(pollution or {})
    return environment


def run_fixture(
    script: Path,
    *args: str,
    pollution: Mapping[str, str] | None = None,
    scenario: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    with launch_fixture(
        script, *args, pollution=pollution, scenario=scenario
    ) as process:
        return wait_for_fixture(process, script.parent.parent)


@contextmanager
def launch_fixture(
    script: Path,
    *args: str,
    pollution: Mapping[str, str] | None = None,
    scenario: Mapping[str, str] | None = None,
) -> Iterator[subprocess.Popen[str]]:
    command = [
        sys.executable,
        str(script),
        "--profile",
        "nightly",
        "--hash-seed",
        "42",
        *args,
    ]
    if scenario is not None:
        scenario_file = script.parent.parent / "scenario-environment.json"
        scenario_file.write_text(json.dumps(dict(scenario)), encoding="utf-8")
        command[1:2] = [str(script.with_name("scenario_runner.py")), str(scenario_file)]
    with subprocess.Popen(
        command,
        cwd=script.parent.parent,
        env=runner_environment(script.parent.parent, pollution),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    ) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                kill_active_process_groups(script.parent.parent)
                process.kill()
                process.wait(timeout=5)


def wait_for_fixture(
    process: subprocess.Popen[str],
    root: Path,
    timeout: float = 60,
    shutdown_timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        with suppress(ProcessLookupError):
            process.send_signal(
                signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
            )
        try:
            stdout, stderr = process.communicate(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            kill_active_process_groups(root)
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        error.output, error.stderr = stdout, stderr
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


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
