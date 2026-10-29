from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock

import pytest

from tests.helpers.fixture_files import copy_fixture
from tests.helpers.installed_package_assertions import process_running
from tests.helpers.platforms import requires_posix
from tests.helpers.test_runner_project import (
    kill_active_process_groups,
    launch_fixture,
    run_fixture,
    runner_environment,
    runner_project,
    wait_for_fixture,
)


def process_stopped(pid: int) -> bool:
    # A reparented POSIX zombie is stopped; the host init owns its final wait.
    with suppress(FileNotFoundError, ProcessLookupError):
        if Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
            return True
    return not process_running(pid)


@pytest.mark.parametrize("read_error", [FileNotFoundError, ProcessLookupError])
@pytest.mark.parametrize("running", [False, True])
def test_process_stopped_rechecks_missing_stat(
    monkeypatch: pytest.MonkeyPatch, read_error: type[OSError], running: bool
) -> None:
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=read_error))
    monkeypatch.setattr(
        sys.modules[__name__], "process_running", Mock(return_value=running)
    )
    assert process_stopped(123) is not running


@pytest.mark.parametrize(
    ("source", "workers", "expected"),
    [
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            0,
            0,
            id="serial-success",
        ),
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            2,
            0,
            id="parallel-success",
        ),
        pytest.param(
            'def test_bad() -> None:\n    raise AssertionError("fixture failure")\n',
            0,
            1,
            id="assertion-failure",
        ),
        pytest.param(
            'raise RuntimeError("collection failed")\n',
            0,
            2,
            id="collection-failure",
        ),
        pytest.param(
            "import os\n\ndef test_crash() -> None:\n    os._exit(7)\n",
            2,
            1,
            id="worker-crash",
        ),
    ],
)
def test_runner_propagates_outcomes_and_retains_diagnostics(
    tmp_path: Path, source: str, workers: int, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(source, encoding="utf-8")
    result = run_fixture(script, "--workers", str(workers))
    assert result.returncode == expected, result.stdout + result.stderr
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    metadata = json.loads((diagnostics / "metadata.json").read_text())
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert metadata["hash_seed"] == 42
    assert metadata["workers"] == workers
    assert metadata["interpreter"] == sys.executable
    assert metadata["dependencies"]["pytest"]
    assert len(metadata["lock_sha256"]) == 64
    assert outcome["exit_code"] == expected
    assert (diagnostics / "pytest.log").stat().st_size > 0
    assert (diagnostics / "junit.xml").is_file()
    assert (diagnostics / "durations.txt").is_file()


@pytest.mark.parametrize(
    "selected_path", [False, True], ids=["discovery", "path-filter"]
)
@pytest.mark.parametrize(("profile", "expected"), [("race", 0), ("nightly", 1)])
def test_runner_selects_marked_tests_after_module_moves(
    tmp_path: Path, selected_path: bool, profile: str, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(
        dedent("""\
            import pytest

            @pytest.mark.race
            def test_interleaving() -> None:
                pass

            def test_unmarked() -> None:
                raise AssertionError("unmarked test ran")
            """),
        encoding="utf-8",
    )
    moved = tmp_path / "new_component" / "test_renamed.py"
    moved.parent.mkdir()
    (tmp_path / "test_example.py").rename(moved)
    selection = [str(moved)] if selected_path else []

    result = run_fixture(script, "--profile", profile, *selection)

    assert result.returncode == expected, result.stdout + result.stderr
    expected_summary = (
        "1 passed, 1 deselected" if profile == "race" else "1 failed, 1 passed"
    )
    assert expected_summary in result.stdout
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    metadata = json.loads((diagnostics / "metadata.json").read_text())
    assert metadata["marker_expression"] == ("race" if profile == "race" else "")
    assert metadata["selection"] == selection


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "def test_ok() -> None:\n    assert True\n",
            5,
            id="empty-selection",
        ),
        pytest.param(
            "import pytest\n\n"
            "@pytest.mark.race\n"
            "def test_race() -> None:\n"
            '    raise AssertionError("marked fixture failure")\n',
            1,
            id="test-failure",
        ),
        pytest.param(
            "import pytest\n\n@pytest.mark.raec\ndef test_typo() -> None:\n    pass\n",
            2,
            id="unknown-marker",
        ),
    ],
)
def test_race_profile_propagates_selection_and_test_failures(
    tmp_path: Path, source: str, expected: int
) -> None:
    script = runner_project(tmp_path)
    (tmp_path / "test_example.py").write_text(source, encoding="utf-8")

    result = run_fixture(script, "--profile", "race")

    assert result.returncode == expected, result.stdout + result.stderr
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["returncode"] == expected
    assert outcome["exit_code"] == expected


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_timeout_terminates_ignoring_child_and_reaps_parent(
    tmp_path, workers
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/blocking_tree.py", tmp_path / "test_example.py")
    copy_fixture("test_runner/session_receipts.py", tmp_path / "conftest.py")
    if os.name == "nt":
        # Withhold cooperative console shutdown to exercise real Job escalation.
        script = copy_fixture(
            "test_runner/force_shutdown_runner.py",
            script.with_name("force_shutdown_runner.py"),
        )
    result = run_fixture(
        script,
        "--workers",
        str(workers),
        "--timeout",
        "5",
        "--shutdown-timeout",
        "0.3",
    )
    assert result.returncode != 0
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert "error" not in outcome, outcome
    assert outcome["pytest"]["timed_out"] is True
    assert outcome["pytest"]["forced"] is True
    sessions = list(tmp_path.glob("session-*.pid"))
    assert len(sessions) == (3 if workers else 1)
    for receipt in [*sessions, tmp_path / "child.pid"]:
        pid = int(receipt.read_text())
        assert process_stopped(pid)


@requires_posix
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_timeout_terminates_isolated_provider(
    tmp_path: Path, workers: int
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/isolated_provider.py", tmp_path / "test_example.py")
    receipt = tmp_path / "provider.pid"
    try:
        result = run_fixture(
            script,
            "--workers",
            str(workers),
            "--timeout",
            "5",
            "--shutdown-timeout",
            "0.3",
        )
        assert result.returncode != 0, result.stdout + result.stderr
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        outcome = json.loads((diagnostics / "outcome.json").read_text())
        assert outcome["pytest"]["timed_out"] is True
        assert outcome["pytest"]["forced"] is True
        pid = int(receipt.read_text())
        deadline = time.monotonic() + 5
        while not process_stopped(pid):
            assert time.monotonic() < deadline, "isolated provider survived shutdown"
            time.sleep(0.05)
        assert not list(diagnostics.glob("*.active-pid"))
    finally:
        if receipt.exists():
            with suppress(ProcessLookupError):
                os.killpg(int(receipt.read_text()), signal.SIGKILL)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux child adoption")
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_runner_reaps_isolated_providers_after_worker_crash(
    tmp_path: Path, workers: int
) -> None:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/orphaned_provider.py", tmp_path / "test_example.py")
    receipts = [tmp_path / name for name in ("provider.pid", "child.pid")]
    try:
        result = run_fixture(script, "--workers", str(workers))
        assert result.returncode == (7 if workers == 0 else 1), (
            result.stdout + result.stderr
        )
        for receipt in receipts:
            assert not process_running(int(receipt.read_text())), (
                f"{receipt.name} survived the worker crash"
            )
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        outcome = json.loads((diagnostics / "outcome.json").read_text())
        assert outcome["pytest"]["forced"] is True
        assert outcome["pytest"]["timed_out"] is False
        assert not list(diagnostics.glob("*.active-pid"))
    finally:
        for receipt in receipts:
            if receipt.exists():
                with suppress(ProcessLookupError):
                    os.killpg(int(receipt.read_text()), signal.SIGKILL)


@pytest.fixture
def blocking_runner(tmp_path: Path) -> Iterator[Path]:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/blocking_tree.py", tmp_path / "test_example.py")
    copy_fixture("test_runner/session_receipts.py", tmp_path / "conftest.py")
    try:
        yield script
    finally:
        kill_active_process_groups(tmp_path)
        for receipt in [*tmp_path.glob("session-*.pid"), tmp_path / "child.pid"]:
            with suppress(FileNotFoundError, ProcessLookupError):
                group_id = os.getpgid(int(receipt.read_text()))
                assert group_id != os.getpgrp()
                os.killpg(group_id, signal.SIGKILL)


def wait_for_receipt(
    launcher: subprocess.Popen[str] | subprocess.Popen[bytes], receipt: Path
) -> None:
    deadline = time.monotonic() + 15
    while not receipt.exists():
        assert launcher.poll() is None, (
            f"launcher exited before {receipt.name} was ready"
        )
        assert time.monotonic() < deadline, f"{receipt.name} did not become ready"
        time.sleep(0.05)


def assert_process_stopped(pid: int) -> None:
    deadline = time.monotonic() + 5
    while not process_stopped(pid):
        assert time.monotonic() < deadline, f"process {pid} survived shutdown"
        time.sleep(0.05)


def assert_runner_tree_stopped(root: Path, workers: int) -> None:
    sessions = list(root.glob("session-*.pid"))
    assert len(sessions) == (3 if workers else 1)
    for receipt in [*sessions, root / "child.pid"]:
        assert_process_stopped(int(receipt.read_text()))


@requires_posix
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_outer_fixture_timeout_interrupts_responsive_launcher(
    tmp_path: Path, blocking_runner: Path, workers: int
) -> None:
    with launch_fixture(
        blocking_runner, "--workers", str(workers), "--shutdown-timeout", "0.3"
    ) as launcher:
        wait_for_receipt(launcher, tmp_path / "child.pid")
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            wait_for_fixture(launcher, tmp_path, timeout=0.1)
        assert launcher.returncode is not None
        assert captured.value.stdout
        assert_runner_tree_stopped(tmp_path, workers)
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        assert (diagnostics / "metadata.json").is_file()
        assert (diagnostics / "pytest.log").is_file()
        outcome = json.loads((diagnostics / "outcome.json").read_text())
        assert "error" not in outcome, outcome
        assert not list(diagnostics.glob("*.active-pid"))
        assert outcome["pytest"]["interrupted"] is True
        assert outcome["pytest"]["forced"] is True


@requires_posix
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_outer_fixture_timeout_kills_unresponsive_launcher(
    tmp_path: Path, blocking_runner: Path, workers: int
) -> None:
    script = copy_fixture(
        "test_runner/unresponsive_runner.py",
        blocking_runner.with_name("unresponsive_runner.py"),
    )
    with launch_fixture(
        script, "--workers", str(workers), "--shutdown-timeout", "0.3"
    ) as launcher:
        wait_for_receipt(launcher, tmp_path / "child.pid")
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            wait_for_fixture(launcher, tmp_path, timeout=0.1, shutdown_timeout=0.1)
        assert launcher.returncode == -signal.SIGKILL
        assert captured.value.stdout
        assert_runner_tree_stopped(tmp_path, workers)
        (diagnostics,) = (tmp_path / "test-results").iterdir()
        assert (diagnostics / "metadata.json").is_file()
        assert (diagnostics / "pytest.log").is_file()
        assert not list(diagnostics.glob("*.active-pid"))


@pytest.fixture
def interruptible_runner(tmp_path: Path) -> Iterator[Path]:
    script = runner_project(tmp_path)
    copy_fixture("test_runner/interruptible_case.py", tmp_path / "test_example.py")
    copy_fixture("test_runner/session_receipts.py", tmp_path / "conftest.py")
    try:
        yield script
    finally:
        kill_active_process_groups(tmp_path)


@contextmanager
def launch_interruption_fixture(script: Path) -> Iterator[subprocess.Popen[bytes]]:
    root = script.parent.parent
    command = [
        sys.executable,
        str(script),
        "--profile",
        "nightly",
        "--shutdown-timeout",
        "1",
    ]
    with (root / "launcher.log").open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=runner_environment(root),
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            yield process
        finally:
            kill_active_process_groups(root)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def interrupt_runner(
    launcher: subprocess.Popen[bytes], ready_receipt: Path, timeout: float = 15
) -> int:
    wait_for_receipt(launcher, ready_receipt)
    launcher.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    return launcher.wait(timeout=timeout)


def test_runner_interrupt_preserves_diagnostics(
    tmp_path: Path, interruptible_runner: Path
) -> None:
    with launch_interruption_fixture(interruptible_runner) as launcher:
        assert interrupt_runner(launcher, tmp_path / "ready") != 0

    (diagnostics,) = (tmp_path / "test-results").iterdir()
    outcome = json.loads((diagnostics / "outcome.json").read_text())
    assert outcome["pytest"]["interrupted"] is True
    assert (diagnostics / "pytest.log").is_file()
    assert (tmp_path / "launcher.log").stat().st_size > 0


@requires_posix
def test_interruption_fixture_timeout_cleans_pytest_group(
    tmp_path: Path, interruptible_runner: Path
) -> None:
    script = copy_fixture(
        "test_runner/unresponsive_runner.py",
        interruptible_runner.with_name("unresponsive_runner.py"),
    )
    with (
        pytest.raises(subprocess.TimeoutExpired),
        launch_interruption_fixture(script) as launcher,
    ):
        interrupt_runner(launcher, tmp_path / "ready", timeout=0.1)

    assert launcher.returncode == -signal.SIGKILL
    (session,) = tmp_path.glob("session-*.pid")
    assert_process_stopped(int(session.read_text()))
    (diagnostics,) = (tmp_path / "test-results").iterdir()
    assert not list(diagnostics.glob("*.active-pid"))
    assert (diagnostics / "metadata.json").is_file()
    assert (diagnostics / "pytest.log").is_file()
    assert (tmp_path / "launcher.log").stat().st_size > 0
